"""Synthetic, offline AH direction diagnostics; no operational data or network."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import f4_ah_metrics as metrics


def row(index=0, *, line=-.5, score=(2, 0), probabilities=(.7, 0, 0, 0, .3),
        odds=(2, 1.9), kickoff="2026-09-28T15:00:00Z"):
    return {
        "match_id": f"synthetic-metrics-{index}", "home_handicap": line,
        "odds": {"home": odds[0], "away": odds[1]}, "final_score": list(score),
        "probabilities": dict(zip(metrics.CLASSES, probabilities)), "kickoff_at": kickoff,
    }


def diagnostic(report, threshold):
    return next(item for item in report["probability_threshold_diagnostics"]
                if item["probability_threshold"] == threshold)


def test_direction_maximizes_positive_probability_independently_of_ev():
    report = metrics.direction_metrics([row(probabilities=(.56, 0, 0, 0, .44), odds=(1.1, 5))])
    record = report["all_rows"]["records"][0]
    assert record["model_direction"]["side"] == "home"
    assert record["model_direction"]["ev"] == pytest.approx(-.384)
    assert report["all_rows"]["selected_count"] == 1
    assert all(item["selected_count"] == 0 for item in report["probability_threshold_diagnostics"])
    assert all(item["model_direction"]["positive_return_hit_rate"] is None
               for item in report["probability_threshold_diagnostics"])


def test_all_five_settlements_remain_separate_and_push_is_not_a_hit():
    rows = [
        row(0, line=-.5, score=(2, 0)),
        row(1, line=-.75, score=(1, 0), probabilities=(.55, .15, 0, 0, .3)),
        row(2, line=-1, score=(1, 0), probabilities=(.7, 0, .1, 0, .2)),
        row(3, line=-.25, score=(1, 1), probabilities=(.7, 0, 0, .1, .2)),
        row(4, line=-.5, score=(0, 1)),
    ]
    report = metrics.direction_metrics(rows)
    group = report["all_rows"]
    model, baseline = group["model_direction"], group["lower_price_heuristic_baseline"]
    assert model["settlement_counts"] == {name: 1 for name in metrics.CLASSES}
    assert model["positive_return_hits"] == 2
    assert model["positive_return_hit_rate"] == .4
    assert model["full_win_rate"] == .2
    assert model["simulated_stake"] == 5
    assert model["roi"] == pytest.approx(0)
    assert model["average_price"] == 2
    assert baseline["settlement_counts"] == {name: 1 for name in metrics.CLASSES}
    assert baseline["roi"] == pytest.approx(-.03)
    assert baseline["average_price"] == pytest.approx(1.9)
    assert group["paired_positive_return_hit_rate_delta"] == 0
    assert group["matched_rows"] == 5
    assert diagnostic(report, .65)["selected_count"] == 5


def test_fixed_thresholds_include_boundary_and_report_valid_row_coverage():
    rows = [row(i, probabilities=(p, 0, 0, 0, 1 - p), odds=(2.1, 2))
            for i, p in enumerate((.54, .55, .60, .65))]
    rows.append(row(4, odds=(1, 2)))
    report = metrics.direction_metrics(rows)
    assert report["input_rows"] == 5 and report["valid_rows"] == 4
    assert report["valid_input_coverage"] == .8
    assert report["rejected_rows"][0]["row_index"] == 4
    for threshold, expected in ((.55, 3), (.60, 2), (.65, 1)):
        group = diagnostic(report, threshold)
        assert group["selected_count"] == expected
        assert group["coverage"] == expected / 4
        assert group["coverage_denominator"] == 4
        assert group["matched_rows"] == expected
        assert group["lower_price_heuristic_baseline"]["count"] == expected
        assert group["paired_positive_return_hit_rate_delta"] == 1


def test_price_baseline_is_paired_on_exact_model_subset_without_own_filter():
    rows = [row(0, odds=(2.2, 1.8)),
            row(1, score=(0, 2), probabilities=(.54, 0, 0, 0, .46), odds=(2.2, 1.8))]
    report = metrics.direction_metrics(rows)
    all_rows, selected = report["all_rows"], diagnostic(report, .55)
    assert all_rows["lower_price_heuristic_baseline"]["positive_return_hit_rate"] == .5
    assert selected["lower_price_heuristic_baseline"]["count"] == 1
    assert selected["lower_price_heuristic_baseline"]["positive_return_hit_rate"] == 0
    assert selected["model_direction"]["positive_return_hit_rate"] == 1
    assert selected["paired_positive_return_hit_rate_delta"] == 1
    assert [record["match_id"] for record in selected["records"]] == [rows[0]["match_id"]]
    # The paired reference remains on the model's exact selected fixture even
    # when the model would abstain from that reference direction on its EV.
    assert selected["records"][0]["lower_price_heuristic_baseline"]["ev"] < 0


def test_away_direction_reverses_probabilities_and_keeps_original_home_line():
    report = metrics.direction_metrics([row(line=-.25, score=(1, 1), odds=(2.2, 1.7),
                                           probabilities=(.2, 0, 0, .2, .6))])
    result = report["all_rows"]["records"][0]["model_direction"]
    assert result["side"] == "away"
    assert result["probabilities"]["full_win"] == .6
    assert result["probabilities"]["half_win"] == .2
    assert result["positive_return_probability"] == pytest.approx(.8)
    assert result["settlement"] == "half_win"
    assert result["net_profit"] == pytest.approx(.35)
    assert result["positive_return_hit"] is True


def test_probability_and_price_ties_both_choose_home_even_for_push():
    report = metrics.direction_metrics([row(line=0, score=(1, 1), odds=(2, 2),
                                           probabilities=(.45, 0, .1, 0, .45))])
    record = report["all_rows"]["records"][0]
    assert record["model_direction"]["side"] == "home"
    assert record["lower_price_heuristic_baseline"]["side"] == "home"
    assert report["all_rows"]["model_direction"]["positive_return_hit_rate"] == 0
    assert report["all_rows"]["model_direction"]["full_win_rate"] == 0
    assert report["all_rows"]["model_direction"]["roi"] == 0


def test_zero_ev_threshold_is_included_but_larger_away_ev_does_not_change_direction():
    report = metrics.direction_metrics([row(probabilities=(.625, 0, 0, 0, .375),
                                           odds=(1.6, 4))])
    selected = diagnostic(report, .60)
    assert selected["selected_count"] == 1
    assert selected["records"][0]["model_direction"]["side"] == "home"
    assert selected["records"][0]["model_direction"]["ev"] == pytest.approx(0)


@pytest.mark.parametrize("mutation", [
    {"probabilities": [float("nan"), 0, 0, 0, .3]},
    {"probabilities": [.7, 0, 0, 0]}, {"probabilities": [.7, .4, 0, 0, .3]},
    {"probabilities": [-.1, 0, 0, 0, 1.1]}, {"probabilities": [True, 0, 0, 0, 0]},
    {"probabilities": None}, {"odds": [1, 2]}, {"odds": [2, float("inf")]},
    {"probabilities": [.5, .1, 0, 0, .4]},
    {"home_handicap": -.3}, {"home_handicap": True},
    {"final_score": [1.0, 0]}, {"final_score": [True, 0]}, {"final_score": [1, -1]},
    {"kickoff_at": "2026-09-28T15:00:00"}, {"match_id": ""},
])
def test_invalid_rows_are_explicitly_excluded_without_nonfinite_output(mutation):
    report = metrics.direction_metrics([{**row(), **mutation}])
    assert report["valid_rows"] == 0
    assert len(report["rejected_rows"]) == 1
    assert json.loads(json.dumps(report, allow_nan=False)) == report


def test_empty_rates_are_none_including_coverage_and_no_invented_hit_rate():
    report = metrics.direction_metrics([])
    assert report["valid_input_coverage"] is None
    for group in [report["all_rows"], *report["probability_threshold_diagnostics"]]:
        assert group["coverage"] is None
        assert group["paired_positive_return_hit_rate_delta"] is None
        assert group["matched_rows"] == 0
        for field in ("model_direction", "lower_price_heuristic_baseline"):
            summary = group[field]
            assert summary["settlement_counts"] == {name: 0 for name in metrics.CLASSES}
            assert summary["positive_return_hit_rate"] is None
            assert summary["full_win_rate"] is None
            assert summary["roi"] is None
            assert summary["average_price"] is None


def test_external_probability_matrix_works_without_mutation_and_alignment_is_strict(tmp_path):
    rows = [row(0), row(1)]
    original = copy.deepcopy(rows)
    matrix = np.array([[.7, 0, 0, 0, .3], [.2, 0, 0, 0, .8]])
    report = metrics.direction_metrics(rows, matrix)
    assert rows == original
    assert report["all_rows"]["records"][1]["model_direction"]["side"] == "away"
    assert matrix.tolist() == [[.7, 0, 0, 0, .3], [.2, 0, 0, 0, .8]]
    destination = tmp_path / "synthetic-metrics.json"
    destination.write_text(json.dumps(report, allow_nan=False), encoding="utf-8")
    assert json.loads(destination.read_text("utf-8")) == report
    with pytest.raises(ValueError, match="one probability vector"):
        metrics.direction_metrics(rows, matrix[:1])
    with pytest.raises(ValueError, match="one probability vector"):
        metrics.direction_metrics([], matrix)


def test_module_import_does_not_eagerly_import_settlement_or_numpy(tmp_path):
    scripts = Path(metrics.__file__).parent
    program = ("import sys; sys.path.insert(0, sys.argv[1]); import f4_ah_metrics; "
               "assert 'f4_ah_research' not in sys.modules; assert 'numpy' not in sys.modules; "
               "assert f4_ah_metrics.direction_metrics([])['valid_rows'] == 0")
    subprocess.run([sys.executable, "-c", program, str(scripts)], cwd=tmp_path,
                   check=True, capture_output=True, text=True)


def test_utc_week_uses_kickoff_after_timezone_conversion():
    report = metrics.direction_metrics([row(kickoff="2026-10-05T00:30:00+08:00")])
    record = report["all_rows"]["records"][0]
    assert record["kickoff_at"] == "2026-10-04T16:30:00+00:00"
    assert record["utc_week"] == "2026-09-28"


def test_paired_week_bootstrap_is_reproducible_and_preserves_fixture_pairing():
    rows = [row(i, kickoff=f"2026-10-{5 + 7 * i:02d}T15:00:00Z") for i in range(3)]
    records = metrics.direction_metrics(rows)["all_rows"]["records"]
    result = metrics.paired_week_bootstrap(records, samples=100, seed=11)
    assert result == metrics.paired_week_bootstrap(records, samples=100, seed=11)
    assert result["utc_week_count"] == 3 and result["matched_rows"] == 3
    assert result["paired_positive_return_hit_rate_delta"] == 1
    assert result["confidence_interval"] == [1, 1]
    assert result["bootstrap_samples"] == 100
    assert result["exploratory"] is True


def test_bootstrap_resamples_whole_weeks_with_fixture_weighted_mean():
    # Week one has one +1 difference; week two has three -1 differences.
    rows = [row(0, kickoff="2026-10-05T15:00:00Z")]
    rows.extend(row(i, score=(0, 2), kickoff="2026-10-12T15:00:00Z") for i in range(1, 4))
    records = metrics.direction_metrics(rows)["all_rows"]["records"]
    result = metrics.paired_week_bootstrap(records, samples=200, seed=5)
    assert result["paired_positive_return_hit_rate_delta"] == -.5
    assert result["confidence_interval"] == [-1, 1]
    assert result["utc_week_count"] == 2


def test_bootstrap_does_not_drop_missing_weeks_or_invent_empty_intervals():
    empty = metrics.paired_week_bootstrap([])
    assert empty["status"] == "empty"
    assert empty["paired_positive_return_hit_rate_delta"] is None
    assert empty["confidence_interval"] is None
    records = metrics.direction_metrics([row(0), row(1)])["all_rows"]["records"]
    assert metrics.paired_week_bootstrap(records)["status"] == "insufficient_utc_weeks"
    records[1]["utc_week"] = None
    result = metrics.paired_week_bootstrap(records)
    assert result["status"] == "missing_utc_weeks"
    assert result["matched_rows"] == 2 and result["missing_utc_week_rows"] == 1
    assert result["confidence_interval"] is None


@pytest.mark.parametrize("parameters", [{"samples": 0}, {"samples": True},
                                         {"confidence": 1}, {"confidence": float("nan")}])
def test_bootstrap_rejects_invalid_settings(parameters):
    with pytest.raises(ValueError):
        metrics.paired_week_bootstrap([], **parameters)
