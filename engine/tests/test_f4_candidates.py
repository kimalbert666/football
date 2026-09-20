"""Offline checks for fixed DC candidates and strictly pre-match f2 observations."""
import copy
import hashlib
import http.client
import io
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

import f4_candidates as fc
from dc_predict import score_matrix


DECISION = "2026-09-20T12:00:00Z"
FIXTURE = {
    "match_id": "espn:test-1", "league": "england-premier",
    "home_id": "arsenal", "away_id": "chelsea", "kickoff_at": "2026-09-20T15:00:00Z",
}
ALIASES = {"england-premier": {
    "arsenal": {"espn": "Arsenal", "variants": ["Gunners"]},
    "chelsea": {"espn": "Chelsea", "variants": ["Blues"]},
}}
ENVELOPE = {
    "model_version": "dc-fixed-test", "frozen_at": "2026-09-20T00:00:00Z",
    "source_sha256": "a" * 64, "training_evidence": "legacy_model_declared_unverified",
    "model": {
        "league": "england-premier", "dateRange": ["2025-08-15", "2026-09-06"],
        "lastFit": "2026-09-10", "homeAdv": .1635, "rho": -.2,
        "teams": {"Arsenal": {"attack": .4462, "defense": -.7262},
                  "Chelsea": {"attack": .5474, "defense": .1676}},
    },
}


def write_model(root, envelope=None, aliases=None):
    path = root / "data/f4/models/frozen.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(ENVELOPE if envelope is None else envelope).encode("utf-8")
    path.write_bytes(body)
    (root / "data/f4/config.json").write_text(json.dumps({
        "candidate_model_path": "data/f4/models/frozen.json",
        "candidate_model_sha256": hashlib.sha256(body).hexdigest(),
    }), encoding="utf-8")
    alias_path = root / "data/01-teams/_aliases.json"
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    alias_path.write_text(json.dumps(ALIASES if aliases is None else aliases), encoding="utf-8")
    return path


def f2_row(**changes):
    return {"home_team": "Arsenal", "away_team": "Chelsea",
            "kickoff_utc": FIXTURE["kickoff_at"], "status": "scheduled",
            "predictions": [{"type": "full-time score", "value": "2:1"}], **changes}


def f2_payload(*rows):
    return {"jsonrpc": "2.0", "result": {"structuredContent": {"matches": list(rows)}}}


def parse(*rows, fixtures=None, aliases=None, captured_at=DECISION):
    return fc.parse_f2(f2_payload(*rows), [FIXTURE] if fixtures is None else fixtures,
                       ALIASES if aliases is None else aliases, captured_at)


def test_dc_matches_existing_raw_seven_by_seven_formula(tmp_path):
    write_model(tmp_path)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    result = fc.dc_candidate(tmp_path, FIXTURE, DECISION)
    model = ENVELOPE["model"]
    home, away = model["teams"]["Arsenal"], model["teams"]["Chelsea"]
    matrix = score_matrix(math.exp(home["attack"] + away["defense"] + model["homeAdv"]),
                          math.exp(away["attack"] + home["defense"]), model["rho"])
    expected = [sum(matrix[x, y] for x in range(7) for y in range(7) if x > y),
                matrix.trace(),
                sum(matrix[x, y] for x in range(7) for y in range(7) if x < y)]
    assert result["p_candidate"] == pytest.approx(expected, abs=1e-14)
    assert all(math.isfinite(value) and value > 0 for value in result["p_candidate"])
    assert sum(result["p_candidate"]) == pytest.approx(1)
    assert result["candidate_version"] == ENVELOPE["model_version"]
    assert result["source_sha256"] == ENVELOPE["source_sha256"]
    assert result["training_evidence"] == "legacy_model_declared_unverified"
    assert result["trained_through"] == "2026-09-10T15:59:59.999999+00:00"
    assert result["reason"] is None
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*.json")}


@pytest.mark.parametrize("change,reason", [
    ({"frozen_at": "2026-09-20T12:00:01Z"}, "newer than decision"),
    ({"lastFit": "2026-09-20"}, "cutoff"),
    ({"dateRange": ["2025-08-15", "2026-09-21"]}, "cutoff"),
    ({"rho": float("nan")}, "non-finite"),
    ({"rho": True}, "non-finite"),
    ({"rho": -.99}, "correction"),
    ({"homeAdv": 1000}, "range"),
    ({"league": "spain-laliga"}, "league mismatch"),
])
def test_dc_rejects_unavailable_or_invalid_frozen_model(tmp_path, change, reason):
    envelope = copy.deepcopy(ENVELOPE)
    target = envelope if "frozen_at" in change else envelope["model"]
    target.update(change)
    write_model(tmp_path, envelope)
    result = fc.dc_candidate(tmp_path, FIXTURE, DECISION)
    assert result["p_candidate"] is None
    assert reason in result["reason"]


@pytest.mark.parametrize("fixture,decision", [
    ({k: v for k, v in FIXTURE.items() if k != "kickoff_at"}, DECISION),
    ({**FIXTURE, "kickoff_at": "2026-09-20"}, DECISION),
    ({**FIXTURE, "kickoff_at": "2026-09-20T15:00:00"}, DECISION),
    (FIXTURE, FIXTURE["kickoff_at"]),
    (FIXTURE, "2026-09-20T15:01:00Z"),
    (FIXTURE, "2026-09-20T12:00:00"),
    ({**FIXTURE, "home_id": "unknown"}, DECISION),
    ({**FIXTURE, "home_id": "Arsenal"}, DECISION),
    ({**FIXTURE, "away_id": "arsenal"}, DECISION),
])
def test_dc_requires_canonical_teams_and_strict_pre_match_times(tmp_path, fixture, decision):
    write_model(tmp_path)
    result = fc.dc_candidate(tmp_path, fixture, decision)
    assert result["p_candidate"] is None
    assert result["reason"]


def test_dc_hash_pin_detects_changed_model(tmp_path):
    path = write_model(tmp_path)
    path.write_bytes(path.read_bytes() + b"\n")
    result = fc.dc_candidate(tmp_path, FIXTURE, DECISION)
    assert result["p_candidate"] is None
    assert "SHA256 mismatch" in result["reason"]


@pytest.mark.parametrize("ambiguity", ["alias", "model"])
def test_dc_rejects_ambiguous_team_mappings(tmp_path, ambiguity):
    envelope, aliases = copy.deepcopy(ENVELOPE), copy.deepcopy(ALIASES)
    if ambiguity == "alias":
        aliases["england-premier"]["chelsea"]["variants"].append("arsenal")
    else:
        envelope["model"]["teams"]["Gunners"] = {"attack": .1, "defense": .1}
    write_model(tmp_path, envelope, aliases)
    result = fc.dc_candidate(tmp_path, FIXTURE, DECISION)
    assert result["p_candidate"] is None
    assert "ambiguous" in result["reason"]


def test_dc_invalid_envelope_is_missing_not_a_crash(tmp_path):
    write_model(tmp_path, {**ENVELOPE, "model": None})
    assert fc.dc_candidate(tmp_path, FIXTURE, DECISION)["p_candidate"] is None


def test_unrelated_alias_conflict_blocks_only_its_own_identity(tmp_path):
    aliases = copy.deepcopy(ALIASES)
    aliases.update({"other-league": {"unrelated": {"espn": "Unrelated Old"}},
                    "another-league": {"unrelated": {"espn": "Unrelated New"}}})
    write_model(tmp_path, aliases=aliases)
    assert fc.dc_candidate(tmp_path, FIXTURE, DECISION)["reason"] is None
    assert parse(f2_row(), aliases=aliases)["predictions"]
    assert parse(f2_row(home_team="Unrelated Old"), aliases=aliases)["predictions"] == {}


def test_real_hash_pinned_model_is_usable_without_training():
    root = Path(__file__).resolve().parents[2]
    result = fc.dc_candidate(root, {**FIXTURE, "kickoff_at": "2026-09-21T15:00:00Z"},
                             "2026-09-21T12:00:00Z")
    assert result["reason"] is None
    assert sum(result["p_candidate"]) == pytest.approx(1)


def test_f2_keeps_only_point_forecast_and_public_metadata():
    secret_text = "raw-provider-news-and-analysis-must-not-be-published"
    result = parse(f2_row(news=secret_text, key_players=secret_text, reasoning=secret_text,
                          kickoff_utc="2026-09-20T23:00:00+08:00"))
    point = result["predictions"][FIXTURE["match_id"]]
    assert point["score"] == [2, 1]
    assert point["direction"] == "H"
    assert point["event_kickoff_at"] == "2026-09-20T15:00:00+00:00"
    assert point["home_id"] == "arsenal" and point["away_id"] == "chelsea"
    assert "probability" not in json.dumps(result)
    assert secret_text not in json.dumps(result)
    assert result["sources"][0]["provider_match_count"] == 1
    assert len(result["sources"][0]["content_sha256"]) == 64
    assert result["problems"] == []


@pytest.mark.parametrize("changes", [
    {"kickoff_utc": "2026-09-20"},
    {"kickoff_utc": "2026-09-20T16:00:00Z"},
    {"kickoff_utc": "2026-09-20T15:05:01Z"},
    {"kickoff_utc": None},
    {"kickoff_utc": None, "kickoff": "2026-09-20T15:00:00"},
    {"home_team": "unknown"},
    {"home_team": "Chelsea", "away_team": "Arsenal"},
    {"status": "finished"}, {"status": "in_progress"}, {"status": "postponed"},
    {"predictions": [{"type": "half_time_score", "value": "1:0"}]},
    {"predictions": [{"type": "full_time_score", "value": "2:1 explanation"}]},
    {"predictions": [{"type": "full_time_score", "value": [True, 1]}]},
    {"predictions": [{"type": "full_time_score", "value": [2.0, 1]}]},
    {"predictions": [{"type": "full_time_score", "value": [-1, 1]}]},
    {"predictions": [{"type": "full_time_score", "value": "100:1"}]},
    {"predictions": "2:1"},
    {"predicted_score": "1:1"},
])
def test_f2_rejects_wrong_identity_time_status_or_score(changes):
    result = parse(f2_row(**changes))
    assert result["predictions"] == {}
    assert result["problems"]


@pytest.mark.parametrize("captured_at", [FIXTURE["kickoff_at"], "2026-09-20T15:01:00Z",
                                        "2026-09-20", "2026-09-20T12:00:00"])
def test_f2_rejects_post_kickoff_or_ambiguous_capture_time(captured_at):
    result = parse(f2_row(), captured_at=captured_at)
    assert result["predictions"] == {}
    assert result["problems"]


def test_f2_rejects_ambiguous_alias_or_fixture():
    aliases = copy.deepcopy(ALIASES)
    aliases["england-premier"]["chelsea"]["variants"].append("Arsenal")
    assert parse(f2_row(), aliases=aliases)["predictions"] == {}
    assert parse(f2_row(), fixtures=[FIXTURE, {**FIXTURE, "match_id": "other"}])["predictions"] == {}


@pytest.mark.parametrize("bad_row", [f2_row(), f2_row(predictions=[]), f2_row(status="finished")])
@pytest.mark.parametrize("bad_first", [False, True])
def test_f2_all_duplicate_identity_rows_remain_missing(bad_row, bad_first):
    rows = [bad_row, f2_row()] if bad_first else [f2_row(), bad_row]
    result = parse(*rows, f2_row())
    assert result["predictions"] == {}
    assert any("duplicate" in problem for problem in result["problems"])


def test_f2_malformed_kickoff_diagnostic_does_not_echo_provider_text():
    secret_text = "private-provider-analysis"
    result = parse(f2_row(kickoff_utc="2026-09-20T15:00:00" + secret_text))
    assert result["predictions"] == {}
    assert secret_text not in json.dumps(result)


@pytest.mark.parametrize("payload", [None, [], {}, {"error": {"message": "service down"}},
                                     {"result": {"isError": True}},
                                     {"result": {"structuredContent": {"matches": "invalid"}}}])
def test_f2_service_errors_do_not_create_forecasts(payload):
    result = fc.parse_f2(payload, [FIXTURE], ALIASES, DECISION)
    assert result["predictions"] == {}
    assert result["problems"]


@pytest.mark.parametrize("error", [URLError("offline"), TimeoutError("timeout"),
                                  HTTPError(fc.ENDPOINT, 503, "unavailable", None, None),
                                  http.client.IncompleteRead(b"partial body")],
                         ids=["network", "timeout", "service", "truncated-response"])
def test_capture_network_errors_are_explicit_missing_data(tmp_path, error):
    write_model(tmp_path)
    fixture = {**FIXTURE, "kickoff_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}

    def fail(request, timeout):
        assert timeout == 20
        raise error

    result = fc.capture_f2(tmp_path, [fixture], opener=fail)
    assert result["captured_at"]
    assert result["predictions"] == {}
    assert result["problems"]


def test_capture_success_requests_only_league_and_uses_point_forecast(tmp_path):
    write_model(tmp_path)
    fixture = {**FIXTURE, "kickoff_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}

    def respond(request, timeout):
        assert json.loads(request.data)["params"] == {
            "name": "get_match_predictions", "arguments": {"league": "premier_league"}}
        return io.BytesIO(json.dumps(f2_payload(f2_row(kickoff_utc=fixture["kickoff_at"]))).encode())

    result = fc.capture_f2(tmp_path, [fixture], opener=respond)
    assert result["predictions"][fixture["match_id"]]["score"] == [2, 1]
    assert result["problems"] == []


def test_capture_does_not_call_service_without_future_fixtures(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("network call should not run")

    result = fc.capture_f2(tmp_path, [{**FIXTURE, "kickoff_at": "2000-01-01T00:00:00Z"}],
                           opener=forbidden)
    assert result["predictions"] == {}
