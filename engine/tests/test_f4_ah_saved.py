"""Synthetic saved evidence must match its version, contract and real times."""
import copy

import pytest

from f4_ah_saved import validate_saved_prediction


CLASSES = ("full_win", "half_win", "push", "half_loss", "full_loss")


def saved():
    vector = [.6, 0, 0, 0, .4]
    row = {
        "match_id": "synthetic-test-only", "experiment_id": "synthetic-experiment",
        "observed_at": "2026-10-06T11:45:00Z",
        "generated_at": "2026-10-06T11:45:01Z", "kickoff_at": "2026-10-06T12:00:00Z",
        "home_handicap": -.5, "odds": [1.9, 1.95], "final_score": [2, 0],
    }
    record = {
        "model_version": "synthetic-model-v1", "hit_rate_direction": "home",
        "prediction": {
            "model_version": "synthetic-model-v1", "observed_at": row["observed_at"],
            "home_handicap": row["home_handicap"],
            "home": {"probabilities": dict(zip(CLASSES, vector)), "decimal_odds": 1.9},
            "away": {"probabilities": dict(zip(CLASSES, reversed(vector))), "decimal_odds": 1.95},
        },
    }
    return row, record


def set_vector(record, vector):
    record["prediction"]["home"]["probabilities"] = dict(zip(CLASSES, vector))
    record["prediction"]["away"]["probabilities"] = dict(zip(CLASSES, reversed(vector)))


def test_valid_saved_output_is_returned_without_mutating_inputs():
    row, record = saved()
    before = copy.deepcopy((row, record))
    prediction, home = validate_saved_prediction(row, record)
    assert prediction is record["prediction"]
    assert home == [.6, 0, 0, 0, .4]
    assert (row, record) == before


def test_old_record_without_new_direction_only_qualifies_as_probability_evidence():
    row, record = saved()
    record.pop("hit_rate_direction")
    validate_saved_prediction(row, record)
    assert "hit_rate_direction" not in record


@pytest.mark.parametrize("change", [
    {"generated_at": "2026-10-06T12:00:00Z"},
    {"generated_at": "2026-10-06T12:30:00Z"},
    {"generated_at": "2026-10-06T11:44:59Z"},
    {"generated_at": "2026-10-06T11:45:01"},
    {"generated_at": None}, {"observed_at": "2026-10-06T11:45:00"},
    {"kickoff_at": "2026-10-06T12:00:00"},
])
def test_generation_must_have_an_explicit_timezone_and_precede_kickoff(change):
    row, record = saved()
    row.update(change)
    with pytest.raises(ValueError):
        validate_saved_prediction(row, record)


def test_generation_at_exact_observation_is_allowed():
    row, record = saved()
    row["generated_at"] = row["observed_at"]
    validate_saved_prediction(row, record)


@pytest.mark.parametrize("change", [
    {"model_version": "different-model"}, {"model_version": None},
    {"observed_at": "2026-10-06T11:46:00Z"}, {"observed_at": "2026-10-06T12:30:00Z"},
    {"home_handicap": -1.5},
])
def test_nested_prediction_must_match_saved_version_observation_and_handicap(change):
    row, record = saved()
    record["prediction"].update(change)
    with pytest.raises(ValueError):
        validate_saved_prediction(row, record)


@pytest.mark.parametrize("version", [None, "", " ", "another-version"])
def test_outer_model_version_must_be_nonempty_and_match_prediction(version):
    row, record = saved()
    record["model_version"] = version
    with pytest.raises(ValueError):
        validate_saved_prediction(row, record)


@pytest.mark.parametrize("side", ["home", "away"])
def test_nested_decimal_prices_must_equal_saved_prices(side):
    row, record = saved()
    record["prediction"][side]["decimal_odds"] = 2.1
    with pytest.raises(ValueError, match="price mismatch"):
        validate_saved_prediction(row, record)


@pytest.mark.parametrize("change", [
    {"home_handicap": -.3}, {"home_handicap": True}, {"home_handicap": float("inf")},
    {"odds": [1, 2]}, {"odds": [1.9, float("nan")]},
    {"final_score": [True, 0]}, {"final_score": [2.0, 0]}, {"final_score": [2, -1]},
])
def test_saved_settlement_contract_and_score_are_validated(change):
    row, record = saved()
    row.update(change)
    with pytest.raises(ValueError):
        validate_saved_prediction(row, record)


@pytest.mark.parametrize("vector", [
    [2, 0, 0, 0, .4], [-.1, 0, 0, 0, 1.1],
    [.6, 0, 0, 0, .3], [float("nan"), 0, 0, 0, .4],
    [float("inf"), 0, 0, 0, .4], [True, 0, 0, 0, 0],
    [.5, .1, 0, 0, .4], [.5, 0, .1, 0, .4], [.5, 0, 0, .1, .4],
])
def test_probabilities_must_be_finite_normalized_and_feasible_for_line(vector):
    row, record = saved()
    set_vector(record, vector)
    with pytest.raises(ValueError):
        validate_saved_prediction(row, record)


@pytest.mark.parametrize("side", ["home", "away"])
def test_exactly_five_named_probabilities_are_required(side):
    row, record = saved()
    probabilities = record["prediction"][side]["probabilities"]
    probabilities.pop("half_win")
    with pytest.raises(ValueError):
        validate_saved_prediction(row, record)
    probabilities["half_win"] = 0
    probabilities["unexpected"] = 0
    with pytest.raises(ValueError):
        validate_saved_prediction(row, record)


def test_away_distribution_must_reverse_home_distribution():
    row, record = saved()
    record["prediction"]["away"]["probabilities"].update(full_win=.5, full_loss=.5)
    with pytest.raises(ValueError, match="reverse"):
        validate_saved_prediction(row, record)


@pytest.mark.parametrize("handicap,vector,direction", [
    (-.25, [.5, 0, 0, .2, .3], "home"),
    (.25, [.1, .5, 0, 0, .4], "home"),
    (0, [.3, 0, .3, 0, .4], "away"),
    (-.5, [.5, 0, 0, 0, .5], "home"),
])
def test_hit_direction_uses_positive_return_probability_with_home_tie_break(handicap, vector, direction):
    row, record = saved()
    row["home_handicap"] = record["prediction"]["home_handicap"] = handicap
    record["hit_rate_direction"] = direction
    set_vector(record, vector)
    validate_saved_prediction(row, record)
    record["hit_rate_direction"] = "away" if direction == "home" else "home"
    with pytest.raises(ValueError, match="hit-rate direction"):
        validate_saved_prediction(row, record)


def test_nested_direction_if_present_must_match_same_saved_policy():
    row, record = saved()
    record["prediction"]["hit_rate_direction"] = "away"
    with pytest.raises(ValueError, match="hit-rate direction"):
        validate_saved_prediction(row, record)


def test_small_rounding_difference_in_probability_vector_is_allowed():
    row, record = saved()
    record["prediction"]["home"]["probabilities"]["full_win"] += 1e-8
    validate_saved_prediction(row, record)
