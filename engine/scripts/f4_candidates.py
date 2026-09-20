"""Frozen raw Dixon-Coles and optional pre-kickoff FootballBin observations.

This module never fits a model, blends probabilities, or writes either store.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from f2_fusion import ENDPOINT


def _aware(value, *, naive_utc=False):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            # Provider fields can contain arbitrary text; diagnostics stay metadata-only.
            raise ValueError("invalid explicit time") from None
    else:
        raise ValueError("an explicit kickoff/decision time is required")
    if parsed.utcoffset() is None:
        if not naive_utc:
            raise ValueError("timezone missing")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _name(value):
    return " ".join(value.casefold().split()) if isinstance(value, str) else ""


def _teams(aliases):
    """Read grouped _aliases.json, or an already flattened canonical map."""
    if not isinstance(aliases, dict):
        raise ValueError("invalid aliases")
    output = {}
    for key, value in aliases.items():
        if key.startswith("_") or not isinstance(value, dict):
            continue
        if any(isinstance(v, dict) for v in value.values()):
            for team_id, detail in value.items():
                if not team_id.startswith("_") and isinstance(detail, dict):
                    if team_id in output and output[team_id] != detail:
                        # An unrelated league conflict must not disable every club.
                        # Keep every conflicting spelling blocked for this identity.
                        records = (output[team_id], detail)
                        variants = [v for record in records for v in record.values()
                                    if isinstance(v, str)]
                        for record in records:
                            if isinstance(record.get("variants"), list):
                                variants.extend(record["variants"])
                        output[team_id] = {"_ambiguous": True, "variants": variants}
                    else:
                        output[team_id] = detail
        else:
            output[key] = value
    return output


def _alias_index(aliases):
    index = {}
    for team_id, details in _teams(aliases).items():
        variants = details.get("variants") or []
        candidates = [team_id, *(v for v in details.values() if isinstance(v, str))]
        if isinstance(variants, list):
            candidates.extend(variants)
        for candidate in candidates:
            normalized = _name(candidate)
            if normalized:
                if details.get("_ambiguous") or (normalized in index and index[normalized] != team_id):
                    index[normalized] = None
                else:
                    index[normalized] = team_id
    return index


def _read_aliases(root):
    return json.loads((Path(root) / "data/01-teams/_aliases.json").read_text(encoding="utf-8"))


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("non-finite or non-numeric model coefficient")
    return float(value)


def dc_candidate(root, fixture, decision_at):
    """Return raw H/D/A from the hash-pinned frozen model, or a missing reason."""
    out = {"p_candidate": None, "candidate_version": None, "trained_through": None,
           "training_evidence": None, "reason": None, "source_sha256": None}
    try:
        root = Path(root).resolve()
        decision = _aware(decision_at)
        if decision >= _aware(fixture["kickoff_at"]):
            raise ValueError("decision is not before kickoff")
        config = json.loads((root / "data/f4/config.json").read_text(encoding="utf-8"))
        model_path = (root / config["candidate_model_path"]).resolve()
        if not model_path.is_relative_to(root / "data/f4/models"):
            raise ValueError("candidate model must be a frozen data/f4/models file")
        body = model_path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest != config["candidate_model_sha256"]:
            raise ValueError("frozen model SHA256 mismatch")
        envelope = json.loads(body)
        out.update(candidate_version=envelope["model_version"],
                   training_evidence=envelope["training_evidence"],
                   source_sha256=envelope["source_sha256"])
        if not isinstance(out["candidate_version"], str) or not out["candidate_version"]:
            raise ValueError("model version missing")
        if not re.fullmatch(r"[0-9a-f]{64}", str(out["source_sha256"])):
            raise ValueError("source SHA256 label missing")
        if _aware(envelope["frozen_at"]) > decision:
            raise ValueError("frozen model is newer than decision")
        model = envelope["model"]
        if model.get("league") != fixture.get("league", "england-premier"):
            raise ValueError("model league mismatch")
        last_data = date.fromisoformat(model["dateRange"][-1])
        last_fit = date.fromisoformat(model["lastFit"])
        cutoff = datetime.combine(max(last_data, last_fit), time.max,
                                  tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
        out["trained_through"] = cutoff.isoformat()
        if cutoff >= decision:
            raise ValueError("declared training cutoff is not before decision")
        index = _alias_index(_read_aliases(root))
        team_ids = (fixture["home_id"], fixture["away_id"])
        if team_ids[0] == team_ids[1] or any(index.get(_name(t)) != t for t in team_ids):
            raise ValueError("unknown or ambiguous canonical fixture team")
        matched = [[key for key in model["teams"] if index.get(_name(key)) == team_id]
                   for team_id in team_ids]
        if any(len(keys) != 1 for keys in matched):
            raise ValueError("frozen model team missing or ambiguous")
        home, away = (model["teams"][keys[0]] for keys in matched)
        lh = math.exp(_finite(home["attack"]) + _finite(away["defense"]) + _finite(model["homeAdv"]))
        la = math.exp(_finite(away["attack"]) + _finite(home["defense"]))
        rho = _finite(model["rho"])
        if not (-1 < rho < 1) or not all(math.isfinite(v) and v > 0 for v in (lh, la)):
            raise ValueError("invalid goal rates or rho")
        tau = {(0, 0): 1 - lh * la * rho, (0, 1): 1 + lh * rho,
               (1, 0): 1 + la * rho, (1, 1): 1 - rho}
        if any(not math.isfinite(v) or v <= 0 for v in tau.values()):
            raise ValueError("non-positive Dixon-Coles correction")
        totals = [0.0, 0.0, 0.0]
        for x in range(7):
            for y in range(7):
                pm = (math.exp(-lh) * lh ** x / math.factorial(x)
                      * math.exp(-la) * la ** y / math.factorial(y))
                probability = max(pm * tau.get((x, y), 1.0), 1e-12)
                totals[0 if x > y else (1 if x == y else 2)] += probability
        total = sum(totals)
        if not math.isfinite(total) or total <= 0:
            raise ValueError("invalid probability mass")
        out["p_candidate"] = [v / total for v in totals]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, OverflowError) as exc:
        out["reason"] = str(exc)
    return out


def _score(value):
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d{1,2})\s*[:-]\s*(\d{1,2})\s*", value)
        return [int(v) for v in match.groups()] if match else None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        if all(type(v) is int and 0 <= v <= 99 for v in value):
            return list(value)
    return None


def _full_time(match):
    values = []
    items = match.get("predictions") or []
    if not isinstance(items, list):
        raise ValueError("invalid predictions array")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid prediction row")
        label = re.sub(r"[-\s]+", "_", str(item.get("type", "")).casefold())
        if "full" in label and ("time" in label or "score" in label):
            values.append(item.get("value"))
    values.extend(match[k] for k in ("full_time_score", "fulltime_score", "predicted_score") if k in match)
    parsed = [_score(value) for value in values]
    if not parsed or any(value is None for value in parsed):
        raise ValueError("full-time point score missing or invalid")
    if any(value != parsed[0] for value in parsed[1:]):
        raise ValueError("conflicting full-time point scores")
    return parsed[0]


def parse_f2(payload, fixtures, aliases, captured_at):
    """Match only exact canonical sides and kickoff within five minutes."""
    out = {"captured_at": None, "sources": [], "predictions": {}, "problems": []}
    try:
        captured = _aware(captured_at)
        out["captured_at"] = captured.isoformat()
        out["sources"] = [{"source": "f2-footballbin", "endpoint": ENDPOINT,
                           "captured_at": captured.isoformat(), "provider_match_count": None,
                           "content_sha256": hashlib.sha256(json.dumps(
                               payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")).hexdigest()}]
        index = _alias_index(aliases)
        result = payload["result"]
        if payload.get("error") or result.get("isError"):
            raise ValueError("FootballBin returned an error")
        matches = result["structuredContent"]["matches"]
        if not isinstance(matches, list):
            raise ValueError("FootballBin matches is not an array")
        out["sources"][0]["provider_match_count"] = len(matches)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        out["problems"].append("f2 response rejected: " + str(exc))
        return out
    eligible = []
    for fixture in fixtures:
        try:
            kickoff = _aware(fixture["kickoff_at"])
            if captured >= kickoff:
                raise ValueError("capture is not before fixture kickoff")
            if any(index.get(_name(fixture[k])) != fixture[k] for k in ("home_id", "away_id")):
                raise ValueError("unknown or ambiguous fixture team")
            eligible.append((fixture, kickoff))
        except (ValueError, KeyError, TypeError) as exc:
            out["problems"].append("f2 fixture rejected: " + str(exc))
    seen = set()
    for number, match in enumerate(matches):
        try:
            home = index.get(_name(match["home_team"]))
            away = index.get(_name(match["away_team"]))
            if not home or not away or home == away:
                raise ValueError("unknown or ambiguous source teams")
            kickoff_key = next((key for key in ("kickoff_utc", "kickoff", "kickoff_formatted")
                                if match.get(key)), None)
            if kickoff_key is None:
                raise ValueError("source kickoff missing")
            kickoff = _aware(match[kickoff_key], naive_utc=kickoff_key == "kickoff_utc")
            if captured >= kickoff:
                raise ValueError("capture is not before source kickoff")
            candidates = [fixture for fixture, when in eligible
                          if fixture["home_id"] == home and fixture["away_id"] == away
                          and abs((when - kickoff).total_seconds()) <= 300]
            if len(candidates) != 1:
                raise ValueError("no unique fixture with matching sides and kickoff")
            fixture = candidates[0]
            match_id = fixture["match_id"]
            if match_id in seen:
                out["predictions"].pop(match_id, None)
                raise ValueError("duplicate source forecast for fixture")
            seen.add(match_id)
            status = re.sub(r"[\s-]+", "_", str(match.get("status") or "").casefold())
            if status not in {"", "scheduled", "not_started", "ns", "upcoming", "pre_match", "prematch", "tbd"}:
                raise ValueError("source status is not pre-match")
            score = _full_time(match)
            out["predictions"][match_id] = {
                "score": score, "direction": "H" if score[0] > score[1] else ("D" if score[0] == score[1] else "A"),
                "captured_at": captured.isoformat(), "published_at": None,
                "source": "f2-footballbin", "event_kickoff_at": kickoff.isoformat(),
                "home_id": home, "away_id": away,
            }
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            out["problems"].append(f"f2 row {number} rejected: {exc}")
    return out


def capture_f2(root, fixtures, *, opener=None):
    """One public request for due fixtures; failures stay explicit and missing."""
    started = datetime.now(timezone.utc)
    out = {"captured_at": started.isoformat(), "sources": [], "predictions": {}, "problems": []}
    due = []
    for fixture in fixtures:
        try:
            if _aware(fixture["kickoff_at"]) > started:
                due.append(fixture)
        except (ValueError, KeyError, TypeError):
            out["problems"].append("f2 fixture has no valid aware kickoff")
    if not due:
        return out
    request = urllib.request.Request(
        ENDPOINT, method="POST", headers={"Content-Type": "application/json", "User-Agent": "football-f4/1.0"},
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "get_match_predictions", "arguments": {"league": "premier_league"}}}).encode("utf-8"))
    try:
        aliases = _read_aliases(root)
        with (opener or urllib.request.urlopen)(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = parse_f2(payload, due, aliases, datetime.now(timezone.utc))
        result["problems"][:0] = out["problems"]
        return result
    except (OSError, ValueError, TypeError, http.client.HTTPException) as exc:
        out["captured_at"] = datetime.now(timezone.utc).isoformat()
        out["problems"].append("f2 capture failed: " + str(exc))
        return out
