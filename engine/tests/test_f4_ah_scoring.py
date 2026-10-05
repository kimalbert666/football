"""Stored synthetic forecasts: policy, version, contract and batch isolation."""
import copy
import json

import pytest

from f4_ah import prospective_scores
from f4_ah_research import CLASSES


def row(index=0, score=(2, 0), candidate="M1", p=.6, experiment="synthetic"):
    probabilities = [p, 0, 0, 0, 1 - p]
    observed = "2026-10-06T11:45:00Z"
    version = candidate + "-synthetic-v2"
    prediction = {"observed_at": observed, "home_handicap": -.5, "model_version": version,
                  "home": {"decimal_odds": 1.9, "probabilities": dict(zip(CLASSES, probabilities))},
                  "away": {"decimal_odds": 1.95, "probabilities": dict(zip(CLASSES, reversed(probabilities)))}}
    return {"match_id": f"synthetic-{index}", "experiment_id": experiment,
            "kickoff_at": "2026-10-06T12:00:00Z", "observed_at": observed,
            "generated_at": "2026-10-06T11:45:01Z", "horizon": "T-15m",
            "home_handicap": -.5, "odds": [1.9, 1.95], "final_score": list(score),
            "bookmaker": "crown", "quote": {"source": "synthetic-only"},
            "candidates": {candidate: {"model_version": version, "prediction": prediction,
                "shadow_direction": "home", "hit_rate_direction": "home" if p >= .5 else "away",
                "hit_rate_policy_version": "positive-return-hit-v1"}}}


def test_saved_models_compared_on_exact_shared_fixtures_not_coverage():
    first, second = row(0), row(1, score=(0, 2))
    other = row(0, candidate="M2", p=.4)
    first["candidates"].update(other["candidates"])
    scored = prospective_scores([first, second])
    assert json.loads(json.dumps(scored, allow_nan=False)) == scored
    left, right = scored["synthetic:M1:M1-synthetic-v2"], scored["synthetic:M2:M2-synthetic-v2"]
    assert left["predicted_matches"] == 2 and right["predicted_matches"] == 1
    comparison = next(iter(left["same_fixture_comparisons"].values()))
    assert comparison["matched_rows"] == 1
    assert comparison["own_hit_rate"] == 1 and comparison["other_hit_rate"] == 0
    assert comparison["paired_positive_return_hit_rate_delta"] == 1


def test_same_match_different_experiment_or_contract_is_not_pooled():
    first = row()
    other_experiment = row(experiment="other")
    other_time = row(candidate="M2")
    other_time["observed_at"] = "2026-10-06T11:44:00Z"
    other_time["candidates"]["M2"]["prediction"]["observed_at"] = other_time["observed_at"]
    scored = prospective_scores([first, other_experiment, other_time])
    assert len(scored) == 3
    assert all(g["predicted_matches"] == 1 for g in scored.values())
    comparison = next(iter(scored["synthetic:M1:M1-synthetic-v2"]["same_fixture_comparisons"].values()))
    assert comparison["matched_rows"] == 0


@pytest.mark.parametrize("mutation", ["late", "version", "probabilities", "prices", "line"])
def test_inconsistent_saved_outputs_are_rejected_and_never_score(mutation):
    item = row()
    prediction = item["candidates"]["M1"]["prediction"]
    if mutation == "late":
        item["generated_at"] = item["kickoff_at"]
    elif mutation == "version":
        prediction["model_version"] = "wrong"
    elif mutation == "probabilities":
        prediction["home"]["probabilities"]["full_win"] = 2
    elif mutation == "prices":
        prediction["home"]["decimal_odds"] = 3
    else:
        prediction["home_handicap"] = -1.5
    group = next(iter(prospective_scores([item]).values()))
    assert group["predicted_matches"] == 0 and len(group["rejected_records"]) == 1
    assert group["log_loss"] is None
    assert group["direction_metrics"]["all_rows"]["model_direction"]["positive_return_hit_rate"] is None


def test_old_probabilities_do_not_become_new_policy_prospective_evidence():
    item = row()
    item["candidates"]["M1"].pop("hit_rate_direction")
    group = next(iter(prospective_scores([item]).values()))
    assert group["predicted_matches"] == 1
    assert not group["direction_metrics"]["policy_prospectively_stored_for_all_rows"]


def test_duplicate_saved_fixture_cannot_inflate_sample_size():
    item = row()
    group = next(iter(prospective_scores([item, copy.deepcopy(item)]).values()))
    assert group["predicted_matches"] == 1
    assert len(group["rejected_records"]) == 1


def test_pair_mappings_and_lists_have_the_same_scoring_contract():
    original = row()
    mapping = copy.deepcopy(original)
    mapping['odds'] = {'home': 1.9, 'away': 1.95}
    mapping['final_score'] = {'home': 2, 'away': 0}
    assert prospective_scores([mapping]) == prospective_scores([original])


def test_simultaneous_batch_drawdown_does_not_depend_on_match_id_order():
    original = [row(i, score=(2, 0) if i < 2 else (0, 2)) for i in range(4)]
    changed = copy.deepcopy(original)
    for item, name in zip(changed, ("a", "c", "b", "d")):
        item["match_id"] = name
    before = next(iter(prospective_scores(original).values()))
    after = next(iter(prospective_scores(changed).values()))
    assert before["max_drawdown"] == pytest.approx(.2)
    assert before["max_drawdown"] == pytest.approx(after["max_drawdown"])
