#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FootballBin (f2) prediction snapshots and guarded f2+f3 calibration.

Commands:
  python f2_fusion.py snapshot [premier_league|champions_league] [matchweek]
  python f2_fusion.py calibrate
  python f2_fusion.py apply H,D,A FULL_TIME_SCORE

The f2 service returns point forecasts rather than calibrated probabilities.  We
therefore treat its full-time direction as a bounded log-pool boost on top of
f3's three-way probabilities.  Weight zero leaves f3 unchanged.  A non-zero
weight is adopted only after chronological holdout validation.
"""
from __future__ import annotations

import json
import math
import re
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from common import ROOT, log

ENDPOINT = "https://ru7m5svay1.execute-api.eu-central-1.amazonaws.com/prod/mcp"
SNAPSHOT_DIR = ROOT / "data" / "05-trends" / "f2"
CORPUS = ROOT / "data" / "04-summaries" / "corpus.json"
CONFIG = ROOT / "engine" / "cache" / "f2_fusion.json"
HISTORY = ROOT / "engine" / "cache" / "f2_fusion_history.json"

MIN_N = 100
MIN_HOLDOUT_N = 30
IMPROVE_MIN = 0.01
WEIGHT_GRID = [round(i * 0.05, 2) for i in range(21)]  # 0.00..1.00


def _default_config() -> dict:
    return {
        "weight": 0.0,
        "status": "collecting",
        "minimumSamples": MIN_N,
        "matchedSamples": 0,
        "lastTuned": None,
        "note": "f2 has no historical forecast endpoint; keep weight 0 until saved pre-match snapshots pass chronological holdout validation",
    }


def _load_config() -> dict:
    if not CONFIG.exists():
        return _default_config()
    return {**_default_config(), **json.loads(CONFIG.read_text(encoding="utf-8"))}


def _write_config(config: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _raw_norm(value: str | None) -> str:
    value = (value or "").casefold()
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value)


def _alias_lookup() -> dict[str, str]:
    path = ROOT / "data" / "01-teams" / "_aliases.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    lookup: dict[str, str] = {}
    for league, teams in raw.items():
        if league.startswith("_") or not isinstance(teams, dict):
            continue
        for team_id, values in teams.items():
            canonical = _raw_norm(team_id)
            candidates = [team_id]
            if isinstance(values, dict):
                candidates.extend(v for v in values.values() if isinstance(v, str))
                candidates.extend(values.get("variants") or [])
            for candidate in candidates:
                if isinstance(candidate, str) and candidate:
                    lookup[_raw_norm(candidate)] = canonical
    return lookup


_ALIASES: dict[str, str] | None = None


def _norm_team(value: str | None) -> str:
    global _ALIASES
    if _ALIASES is None:
        _ALIASES = _alias_lookup()
    raw = _raw_norm(value)
    return _ALIASES.get(raw, raw)


def _score(value: str | None) -> tuple[int, int] | None:
    m = re.search(r"(\d+)\s*[:\-]\s*(\d+)", str(value or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _outcome(score: tuple[int, int]) -> int:
    return 0 if score[0] > score[1] else (1 if score[0] == score[1] else 2)


def _rps(probs: list[float], outcome_idx: int) -> float:
    observed = [0.0, 0.0, 0.0]
    observed[outcome_idx] = 1.0
    return 0.5 * sum(
        (sum(probs[:k + 1]) - sum(observed[:k + 1])) ** 2 for k in range(2)
    )


def combine(probs: list[float], f2_outcome: int, weight: float) -> list[float]:
    """Boost f2's categorical direction in log space, preserving calibration."""
    if weight == 0.0:
        return list(probs)
    logits = [math.log(max(float(p), 1e-12)) for p in probs]
    logits[f2_outcome] += weight
    peak = max(logits)
    values = [math.exp(v - peak) for v in logits]
    total = sum(values)
    return [v / total for v in values]


def _f2_full_time(match: dict) -> tuple[int, int] | None:
    for item in match.get("predictions") or []:
        label = str(item.get("type") or "").casefold().replace("-", "_").replace(" ", "_")
        if "full" in label and ("time" in label or "score" in label):
            parsed = _score(item.get("value"))
            if parsed:
                return parsed
    for key in ("full_time_score", "fulltime_score", "predicted_score"):
        parsed = _score(match.get(key))
        if parsed:
            return parsed
    return None


def _kickoff_date(match: dict, fallback: str) -> str:
    raw = match.get("kickoff_utc") or match.get("kickoff") or match.get("kickoff_formatted")
    if raw:
        m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", str(raw))
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return fallback


def snapshot(league: str, matchweek: int | None = None) -> Path | None:
    arguments: dict[str, object] = {"league": league}
    if matchweek is not None:
        arguments["matchweek"] = matchweek
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_match_predictions", "arguments": arguments},
    }
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "football-f2-fusion/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # network failure must not corrupt the learning store
        log("f2-fusion", f"snapshot failed for {league}: {exc}")
        return None
    result = raw.get("result") or {}
    structured = result.get("structuredContent")
    if result.get("isError") or not structured or not structured.get("matches"):
        message = ((result.get("content") or [{}])[0].get("text") or "no predictions")
        log("f2-fusion", f"snapshot skipped for {league}: {message}")
        return None
    captured_at = datetime.now(timezone.utc)
    captured = captured_at.isoformat()
    output = {
        "source": "f2-footballbin",
        "capturedAt": captured,
        "league": structured.get("league") or league,
        "matchweek": structured.get("matchweek") or matchweek,
        "matches": structured.get("matches"),
    }
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = f"-mw{output['matchweek']}" if output.get("matchweek") is not None else ""
    stamp = captured_at.strftime("%Y-%m-%dT%H%M%SZ")
    path = SNAPSHOT_DIR / f"{stamp}-{league}{suffix}.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log("f2-fusion", f"saved {len(output['matches'])} pre-match predictions to {path.relative_to(ROOT)}")
    return path


def _corpus_index() -> dict[tuple[str, str, str], tuple[list[float], int]]:
    if not CORPUS.exists():
        return {}
    records = json.loads(CORPUS.read_text(encoding="utf-8")).get("records") or []
    index: dict[tuple[str, str, str], tuple[list[float], int]] = {}
    for row in records:
        result = _score(row.get("result"))
        probs = row.get("p_final")
        match = str(row.get("match") or "")
        if not result or not isinstance(probs, list) or len(probs) != 3 or " vs " not in match:
            continue
        home, away = match.split(" vs ", 1)
        key = (str(row.get("date") or ""), _norm_team(home), _norm_team(away))
        index[key] = ([float(p) for p in probs], _outcome(result))
    return index


def load_pairs() -> list[tuple[str, list[float], int, int]]:
    index = _corpus_index()
    pairs: list[tuple[str, list[float], int, int]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in sorted(SNAPSHOT_DIR.glob("*.json")) if SNAPSHOT_DIR.exists() else []:
        data = json.loads(path.read_text(encoding="utf-8"))
        fallback = str(data.get("capturedAt") or path.name[:10])[:10]
        for match in data.get("matches") or []:
            predicted_score = _f2_full_time(match)
            if not predicted_score:
                continue
            key = (
                _kickoff_date(match, fallback),
                _norm_team(match.get("home_team")),
                _norm_team(match.get("away_team")),
            )
            if key in seen or key not in index:
                continue
            seen.add(key)
            probs, actual = index[key]
            pairs.append((key[0], probs, _outcome(predicted_score), actual))
    return sorted(pairs, key=lambda row: row[0])


def calibrate() -> bool:
    pairs = load_pairs()
    config = _load_config()
    config["matchedSamples"] = len(pairs)
    if len(pairs) < MIN_N:
        config.update({"weight": 0.0, "status": "collecting", "lastChecked": date.today().isoformat()})
        _write_config(config)
        log("f2-fusion", f"matched {len(pairs)}/{MIN_N}; keep f2 weight at 0")
        return False
    split = max(MIN_N - MIN_HOLDOUT_N, int(len(pairs) * 0.7))
    split = min(split, len(pairs) - MIN_HOLDOUT_N)
    train, holdout = pairs[:split], pairs[split:]

    def mean_rps(rows, weight: float) -> float:
        return sum(_rps(combine(p, f2, weight), actual) for _, p, f2, actual in rows) / len(rows)

    best_weight = min(WEIGHT_GRID, key=lambda w: mean_rps(train, w))
    base_rps = mean_rps(holdout, 0.0)
    tuned_rps = mean_rps(holdout, best_weight)
    improvement = (base_rps - tuned_rps) / base_rps if base_rps else 0.0
    if best_weight == 0.0 or improvement < IMPROVE_MIN:
        config.update({
            "weight": 0.0,
            "status": "rejected_by_holdout",
            "lastChecked": date.today().isoformat(),
            "holdoutN": len(holdout),
            "holdoutRpsBaseline": round(base_rps, 6),
            "holdoutRpsCandidate": round(tuned_rps, 6),
            "holdoutImprovement": round(improvement, 6),
        })
        _write_config(config)
        log("f2-fusion", f"candidate w={best_weight} holdout improvement {improvement:+.2%}; keep weight 0")
        return False
    old = float(config.get("weight") or 0.0)
    history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else []
    history.append({
        "date": date.today().isoformat(), "old": old, "new": best_weight,
        "n": len(pairs), "holdoutN": len(holdout),
        "rpsBaseline": round(base_rps, 6), "rpsNew": round(tuned_rps, 6),
        "improvement": round(improvement, 6),
    })
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    config.update({
        "weight": best_weight, "status": "active", "lastTuned": date.today().isoformat(),
        "matchedSamples": len(pairs), "holdoutN": len(holdout),
        "holdoutRpsBaseline": round(base_rps, 6), "holdoutRpsNew": round(tuned_rps, 6),
        "holdoutImprovement": round(improvement, 6),
    })
    _write_config(config)
    log("f2-fusion", f"activated f2 weight {old}->{best_weight}; holdout RPS improvement {improvement:+.2%}")
    return True


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"
    if command == "snapshot":
        leagues = [sys.argv[2]] if len(sys.argv) > 2 else ["premier_league", "champions_league"]
        matchweek = int(sys.argv[3]) if len(sys.argv) > 3 else None
        for league in leagues:
            snapshot(league, matchweek)
    elif command == "calibrate":
        calibrate()
    elif command == "apply" and len(sys.argv) >= 4:
        probs = [float(v) for v in sys.argv[2].split(",")]
        parsed = _score(sys.argv[3])
        if len(probs) != 3 or not parsed:
            raise SystemExit("apply requires H,D,A and a score such as 2:1")
        config = _load_config()
        print(json.dumps({
            "f3": probs,
            "f2Score": sys.argv[3],
            "weight": config["weight"],
            "fused": combine(probs, _outcome(parsed), float(config["weight"])),
            "status": config["status"],
        }, ensure_ascii=False, indent=2))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
