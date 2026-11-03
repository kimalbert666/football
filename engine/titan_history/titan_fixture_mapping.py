"""Offline candidate matching between independent ESPN and Titan fixture lists.

This only proposes identities to verify on the AH detail page. It never verifies
an odds page, infers team aliases, assigns a missing timezone, or chooses among
duplicate rows. Every supplied expected fixture receives a coverage row, even
when its input is invalid. The caller must supply the separately checked Titan
league ID; this module does not infer it from team names or an event number.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import re


LEAGUE = "england-premier"


def _identifier(value):
    return value if isinstance(value, str) and value and value == value.strip() else None


def _numeric_id(value):
    # Provider adapters currently return strings. Integers can be accepted
    # without accepting booleans or strings with silent whitespace changes.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    return value if re.fullmatch(r"[1-9][0-9]*", value) else None


def _instant(value):
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _stamp(value):
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _normalized(row, *, expected, titan_league_id):
    issues = []
    if not isinstance(row, dict):
        return {"issues": ["input_is_not_an_object"], "key": None, "id": None,
                "home_id": None, "away_id": None, "kickoff": None, "raw": {}}
    home, away = _identifier(row.get("home_id")), _identifier(row.get("away_id"))
    if not home or not away or home == away:
        issues.append("canonical_home_away_missing_or_invalid")
    kickoff = _instant(row.get("kickoff_at"))
    if kickoff is None:
        issues.append("kickoff_missing_invalid_or_timezone_naive")
    if expected:
        identity = _identifier(row.get("match_id"))
        source_id = _numeric_id(row.get("source_event_id"))
        if row.get("provider") != "espn" or source_id is None or identity != "espn:" + source_id:
            issues.append("espn_fixture_id_conflict_or_missing")
        if row.get("league") != LEAGUE:
            issues.append("fixture_league_mismatch")
        if row.get("problems"):
            issues.append("fixture_has_source_problems")
        if row.get("status") not in {"scheduled", "completed", "live"}:
            issues.append("fixture_status_not_supported")
    else:
        identity = _numeric_id(row.get("provider_event_id"))
        if identity is None:
            issues.append("titan_event_id_missing_or_invalid")
        if _numeric_id(row.get("league_id")) != titan_league_id:
            issues.append("titan_league_mismatch")
    # Keep a structural key even for rejected records to expose collisions.
    key = (home, away, kickoff) if home and away and home != away and kickoff else None
    return {"issues": issues, "key": key, "id": identity, "home_id": home,
            "away_id": away, "kickoff": kickoff, "raw": row}


def map_fixture_candidates(fixtures, titan_candidates, *, titan_league_id):
    """Return coverage plus unverified, uniquely matched Titan candidates.

    ``fixtures`` are standard f4_sources ESPN fixtures; ``titan_candidates``
    are f4_crown.parse_index rows. Input lists must represent the caller's
    chosen scope. No fixture is silently filtered out. The expected count is
    the number of supplied rows, with unique match IDs also reported so input
    duplication cannot masquerade as additional matches.

    Times with explicit different offsets may represent the same instant.
    Otherwise even a one-second kickoff discrepancy fails matching. The
    same teams on another date are reported as a mismatch, never substituted.
    """
    league_id = _numeric_id(titan_league_id)
    if league_id is None:
        raise ValueError("separately verified numeric Titan league ID required")
    if not isinstance(fixtures, (list, tuple)) or not isinstance(titan_candidates, (list, tuple)):
        raise TypeError("fixture and candidate inputs must be lists or tuples")
    expected = [_normalized(row, expected=True, titan_league_id=league_id) for row in fixtures]
    candidates = [_normalized(row, expected=False, titan_league_id=league_id) for row in titan_candidates]
    fixture_ids = Counter(row["id"] for row in expected if row["id"] is not None)
    fixture_keys = Counter(row["key"] for row in expected if row["key"] is not None)
    candidate_ids = Counter(row["id"] for row in candidates if row["id"] is not None)
    by_key, by_sides = defaultdict(list), defaultdict(list)
    for index, row in enumerate(candidates):
        if row["key"] is not None:
            by_key[row["key"]].append(index)
        if row["home_id"] and row["away_id"]:
            by_sides[(row["home_id"], row["away_id"])].append(index)
    coverage, used = [], set()
    for index, fixture in enumerate(expected):
        exact = by_key.get(fixture["key"], []) if fixture["key"] is not None else []
        row = {"fixture_input_index": index, "match_id": fixture["id"],
               "home_id": fixture["home_id"], "away_id": fixture["away_id"],
               "kickoff_at": _stamp(fixture["kickoff"]), "state": "missing",
               "mapping_verified": False, "requires_ah_detail_validation": True,
               "candidate": None, "candidate_input_indices": list(exact),
               "reasons": list(fixture["issues"])}
        duplicate_fixture_id = fixture["id"] is not None and fixture_ids[fixture["id"]] > 1
        duplicate_fixture_key = fixture["key"] is not None and fixture_keys[fixture["key"]] > 1
        if duplicate_fixture_id or duplicate_fixture_key:
            row["state"] = "ambiguous"
            if duplicate_fixture_id:
                row["reasons"].append("duplicate_expected_match_id")
            if duplicate_fixture_key:
                row["reasons"].append("duplicate_expected_fixture_identity")
        elif row["reasons"]:
            pass
        elif len(exact) > 1:
            row["state"] = "ambiguous"
            row["reasons"].append("multiple_titan_rows_for_fixture")
        elif len(exact) == 1:
            candidate_index = exact[0]
            candidate = candidates[candidate_index]
            if candidate["id"] is not None and candidate_ids[candidate["id"]] > 1:
                row["state"] = "ambiguous"
                row["reasons"].append("duplicate_or_conflicting_titan_event_id")
            elif candidate["issues"]:
                row["reasons"].extend(candidate["issues"])
            else:
                raw = candidate["raw"]
                row["state"] = "mapped_candidate"
                row["candidate"] = {"provider_event_id": candidate["id"], "league_id": league_id,
                                    "home_id": candidate["home_id"], "away_id": candidate["away_id"],
                                    "home": raw.get("home"), "away": raw.get("away"),
                                    "kickoff_at": _stamp(candidate["kickoff"])}
                used.add(candidate_index)
        else:
            related = by_sides.get((fixture["home_id"], fixture["away_id"]), [])
            if related:
                row["candidate_input_indices"] = list(related)
                row["reasons"].append("same_teams_but_kickoff_mismatch")
            elif by_sides.get((fixture["away_id"], fixture["home_id"])):
                row["reasons"].append("only_reversed_home_away_candidates")
            else:
                row["reasons"].append("no_matching_titan_candidate")
        coverage.append(row)
    diagnostics = []
    for index, candidate in enumerate(candidates):
        reasons = list(candidate["issues"])
        if candidate["id"] is not None and candidate_ids[candidate["id"]] > 1:
            reasons.append("duplicate_or_conflicting_titan_event_id")
        diagnostics.append({"candidate_input_index": index, "provider_event_id": candidate["id"],
                            "selected_as_candidate": index in used, "issues": reasons})
    states = Counter(row["state"] for row in coverage)
    return {"schema_version": "titan-fixture-candidate-map-v1",
            "league_contract": {"canonical_league": LEAGUE, "titan_league_id": league_id},
            "mapping_verified": False, "formal_observation": False, "training_eligible": False,
            "counts": {"expected": len(fixtures), "unique_expected_match_ids": len(fixture_ids),
                       "mapped": states["mapped_candidate"], "missing": states["missing"],
                       "ambiguous": states["ambiguous"], "candidate_rows": len(candidates),
                       "unused_candidate_rows": len(candidates) - len(used)},
            "coverage": coverage, "candidate_diagnostics": diagnostics}
