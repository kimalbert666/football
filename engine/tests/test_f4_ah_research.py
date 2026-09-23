"""Settlement, frozen-time feature eligibility and train/validation/test isolation."""
import copy
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import f4_ah_research as ah


BASE = datetime(2025, 1, 1, 15, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def row(index=0, handicap=-.25):
    kickoff = BASE + timedelta(days=index)
    return {"match_id": f"synthetic-{index}", "kickoff_at": kickoff.isoformat(),
            "observed_at": (kickoff - timedelta(minutes=15)).isoformat(),
            "settled_at": (kickoff + timedelta(hours=3)).isoformat(),
            "provenance": {"source": "synthetic-test-only"},
            "home_handicap": handicap, "odds": {"home": 1.9, "away": 2.0},
            "p_1x2": [.48, .27, .25], "final_score": [index % 4, (index // 3) % 3],
            "ratings": {"home": 1500 + index % 100, "away": 1500,
                        "observed_at": (kickoff - timedelta(hours=12)).isoformat(),
                        "source": "synthetic-test-only", "prior_games": 30},
            "early_quote": {"observed_at": (kickoff - timedelta(hours=3)).isoformat(),
                            "home_handicap": handicap + .25,
                            "odds": {"home": 1.95, "away": 1.95},
                            "provenance": "synthetic-test-only"}}


def sample(n=100):
    return [row(i, [-2.25, -1.75, -1.5, -1, -.75, -.5, -.25, 0, .25, .75][i % 10])
            for i in range(n)]


def run(rows, **kwargs):
    return ah.research(rows, now=NOW, min_train=30, min_validation=10, min_test=10, **kwargs)


@pytest.mark.parametrize("score,line,settlement,profit", [
    ([1, 1], -.25, "half_loss", -.5), ([1, 1], .25, "half_win", .45),
    ([1, 0], -.75, "half_win", .45), ([1, 0], -1.25, "half_loss", -.5),
    ([2, 0], -1.25, "full_win", .9), ([1, 0], -1, "push", 0),
    ([0, 2], 1.75, "half_loss", -.5), ([0, 2], 2.25, "half_win", .45),
    ([30, 0], -30.25, "half_loss", -.5), ([0, 0], 20.5, "full_win", .9),
])
def test_settlement_every_line_and_deep_lines(score, line, settlement, profit):
    result = ah.settle_asian_handicap(*score, line, "home", 1.9)
    assert result["settlement"] == settlement
    assert result["net_profit"] == pytest.approx(profit)


def test_all_quarter_lines_are_symmetric_and_no_score_cutoff():
    for quarters in range(-60, 61):
        for difference in (-31, -2, -1, 0, 1, 2, 31):
            score = [max(difference, 0), max(-difference, 0)]
            home = ah.settle_asian_handicap(*score, quarters / 4, "home", 2)
            away = ah.settle_asian_handicap(*score, quarters / 4, "away", 2)
            assert home["net_profit"] == -away["net_profit"]
            assert ah.CLASSES.index(home["settlement"]) + ah.CLASSES.index(away["settlement"]) == 4
            assert ah._mask(quarters / 4)[ah.CLASSES.index(home["settlement"])]


@pytest.mark.parametrize("args", [
    (True, 0, -.5, "home", 2), (1.0, 0, -.5, "home", 2),
    (1, -1, -.5, "home", 2), (1, 0, -.3, "home", 2),
    (1, 0, float("nan"), "home", 2), (1, 0, -.5, "H", 2),
    (1, 0, -.5, "home", .9), (1, 0, -.5, "home", float("inf")),
])
def test_invalid_settlement_rejected(args):
    with pytest.raises(ValueError):
        ah.settle_asian_handicap(*args)


def test_insufficient_samples_do_not_invent_models(tmp_path):
    destination = tmp_path / "new-experiment"
    report = ah.research([row()], now=NOW, output_dir=destination)
    assert report["status"] == "insufficient_data"
    assert report["weight"] == 0 and report["auto_promote"] is False
    assert all(c["model"] is None for c in report["candidates"].values())
    assert json.loads((destination / "report.json").read_text("utf-8")) == report
    with pytest.raises(FileExistsError):
        ah.research([], now=NOW, output_dir=destination)


@pytest.mark.parametrize("mutation", [
    {"observed_at": BASE.isoformat()}, {"observed_at": "2025-01-01T12:00:00"},
    {"settled_at": "2027-01-01T00:00:00Z"}, {"settled_at": BASE.isoformat()},
    {"provenance": None}, {"p_1x2": [.5, .5, .5]}, {"odds": [1, 2]},
    {"final_score": [1, True]}, {"home_handicap": -.3},
])
def test_bad_input_and_temporal_leakage_excluded(mutation):
    report = run([{**row(), **mutation}])
    assert report["accepted_matches"] == 0
    assert len(report["rejected_rows"]) == 1


@pytest.fixture(scope="module")
def fitted():
    return run(sample())


def test_real_optimizer_produces_three_serializable_frozen_candidates(fitted):
    assert fitted["status"] == "trained_shadow"
    assert json.loads(json.dumps(fitted, allow_nan=False)) == fitted
    for candidate, item in fitted["candidates"].items():
        assert item["status"] == "trained_shadow", item["reason"]
        assert item["counts"] == {"train": 60, "validation": 20, "test": 20}
        assert item["model"]["candidate_id"] == candidate
        assert item["model"]["weight"] == 0 and item["weight"] == 0
        assert item["test"]["count"] == 20
        assert item["test"]["prospective_evidence"] is False
        assert len(item["regularization_search"]) == 3
        assert item["test"]["ev_threshold_diagnostic"]["actionable"] is False


def test_final_test_outcomes_cannot_change_fit_scaling_or_parameter_selection(fitted):
    rows = sample()
    for item in rows[80:]:
        item["final_score"] = [12, 0]
    changed = run(rows)
    for candidate in ah.CANDIDATES:
        before, after = fitted["candidates"][candidate], changed["candidates"][candidate]
        assert before["model"] == after["model"]
        assert before["validation"] == after["validation"]
        assert before["test"]["log_loss"] != after["test"]["log_loss"]


def test_training_scaling_ignores_validation_and_test_features(fitted):
    model = fitted["candidates"]["M1"]["model"]
    matrix = np.array([ah._features(r, "M1") for r in sample()[:60]])
    assert model["mean"] == pytest.approx(matrix.mean(axis=0))
    assert model["trained_through"] == sample()[79]["settled_at"]


def test_missing_ratings_does_not_prevent_market_or_movement_model():
    rows = sample()
    for item in rows:
        item.pop("ratings")
    report = run(rows)
    assert report["candidates"]["M1"]["status"] == "trained_shadow"
    assert report["candidates"]["M2"]["status"] == "insufficient_data"
    assert report["candidates"]["M3"]["status"] == "trained_shadow"


def test_future_ratings_and_fake_opening_snapshot_are_not_features():
    rows = sample()
    for item in rows:
        item["ratings"]["observed_at"] = item["kickoff_at"]
        item["early_quote"]["observed_at"] = item["observed_at"]
    report = run(rows)
    assert report["candidates"]["M1"]["status"] == "trained_shadow"
    for candidate in ("M2", "M3"):
        assert report["candidates"][candidate]["model"] is None
        assert report["candidates"][candidate]["counts"]["train"] == 0


def test_same_match_snapshots_and_same_kickoff_batch_never_cross_partitions():
    rows = sample(25)
    for index, item in enumerate(rows):
        when = BASE + timedelta(days=index // 3)
        item.update(kickoff_at=when.isoformat(),
                    observed_at=(when - timedelta(minutes=15)).isoformat(),
                    settled_at=(when + timedelta(hours=3)).isoformat())
    duplicate = copy.deepcopy(rows[3])
    duplicate["observed_at"] = (ah._time(duplicate["observed_at"]) - timedelta(minutes=40)).isoformat()
    report = run(rows + [duplicate])
    assert report["duplicate_snapshots_removed"] == 1
    splits = [report["split"][name] for name in ("train", "validation", "test")]
    ids = [set(part["match_ids"]) for part in splits]
    assert not ids[0] & ids[1] and not ids[1] & ids[2]
    assert ah._time(splits[0]["kickoff_to"]) < ah._time(splits[1]["kickoff_from"])
    assert ah._time(splits[1]["kickoff_to"]) < ah._time(splits[2]["kickoff_from"])


def test_conflicting_duplicate_outcomes_exclude_fixture_entirely():
    conflicting = {**row(), "final_score": [8, 8]}
    report = run([row(), conflicting])
    assert report["accepted_matches"] == 0
    assert "conflicting" in report["rejected_rows"][0]["reason"]


def test_late_available_labels_are_purged_at_split_boundary():
    rows = sample(20)
    rows[10]["settled_at"] = rows[13]["settled_at"]
    rows[14]["settled_at"] = rows[17]["settled_at"]
    report = run(rows)
    assert report["split"]["purged"] == {"train": 1, "validation": 1}
    assert rows[10]["match_id"] not in report["split"]["train"]["match_ids"]
    assert rows[14]["match_id"] not in report["split"]["validation"]["match_ids"]


def test_public_predict_forbids_backdated_models_and_preserves_settlement_logic(fitted):
    model = fitted["candidates"]["M1"]["model"]
    with pytest.raises(ValueError, match="did not exist"):
        ah.predict(model, row(90))
    future = row(370, handicap=-20.25)
    output = ah.predict(model, future)
    assert output["selected"] is False and output["weight"] == 0
    p = output["home"]["probabilities"]
    assert sum(p.values()) == pytest.approx(1)
    assert p["push"] == 0 and p["half_win"] == 0
    assert output["away"]["probabilities"]["full_win"] == p["full_loss"]
    expected = p["full_win"] * .9 - p["half_loss"] * .5 - p["full_loss"]
    assert output["home"]["ev"] == pytest.approx(expected)


def test_prediction_without_result_is_valid_and_input_is_not_mutated(fitted):
    future = row(370)
    future.pop("final_score")
    future.pop("settled_at")
    original = copy.deepcopy(future)
    for candidate in ah.CANDIDATES:
        result = ah.predict(fitted["candidates"][candidate]["model"], future)
        assert sum(result["away"]["probabilities"].values()) == pytest.approx(1)
    assert future == original
