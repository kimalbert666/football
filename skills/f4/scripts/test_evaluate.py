"""Run with: python -m unittest discover -s <this directory> -p test_evaluate.py."""
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evaluate import evaluate, read_rows, scores, validate


def row(**changes):
    record = {
        "match_id": "m1", "league": "EPL", "horizon": "T-60m", "model_version": "v1",
        "kickoff_at": "2026-09-19T15:00:00Z", "decision_at": "2026-09-19T14:00:00Z",
        "captured_at": "2026-09-19T13:59:00Z", "trained_through": "2026-09-01T00:00:00Z",
        "outcome": "H", "p_baseline": [0.5, 0.3, 0.2],
        "p_candidate": [0.7, 0.2, 0.1], "selected": True,
    }
    record.update(changes)
    return record


class EvaluationTests(unittest.TestCase):
    def test_metric_definitions_and_tie_order(self):
        perfect = scores([1, 0, 0], "H")
        self.assertEqual(perfect, dict(rps=0, brier=0, log_loss=0, accuracy=1))
        wrong = scores([1, 0, 0], "A")
        self.assertEqual(wrong["rps"], 1)
        self.assertEqual(wrong["brier"], 2)
        self.assertAlmostEqual(wrong["log_loss"], -math.log(1e-15))
        intermediate = scores([0.5, 0.3, 0.2], "D")
        self.assertAlmostEqual(intermediate["rps"], 0.145)
        self.assertAlmostEqual(intermediate["brier"], 0.78)
        self.assertEqual(scores([0.5, 0.5, 0], "H")["accuracy"], 1)
        self.assertEqual(scores([0, 0.5, 0.5], "D")["accuracy"], 1)

    def test_selected_baseline_uses_identical_subset_and_pending_counts(self):
        records = [row(), row(match_id="m2", outcome="A", selected=False),
                   row(match_id="m3", outcome=None)]
        result = evaluate(records, bootstrap=10)
        self.assertEqual(result["coverage"]["n_total"], 3)
        self.assertEqual(result["coverage"]["n_settled"], 2)
        self.assertEqual(result["coverage"]["n_pending"], 1)
        self.assertEqual(result["coverage"]["selected_n_pending"], 1)
        self.assertEqual(result["coverage"]["selected_fraction_settled"], 0.5)
        selected = result["metrics"]["selected_settled"]
        self.assertEqual(selected["baseline"]["n"], selected["candidate"]["n"])
        self.assertEqual(selected["baseline"]["n"], 1)
        self.assertAlmostEqual(selected["baseline"]["rps"], 0.145)
        self.assertAlmostEqual(selected["candidate"]["rps"], 0.05)
        self.assertAlmostEqual(result["metrics"]["all_settled"]["baseline"]["accuracy"], 0.5)

    def test_pending_only_is_valid_but_unscored_and_still_validated(self):
        result = evaluate([row(outcome=None)])
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["metrics"]["all_settled"]["candidate"])
        self.assertIsNone(result["coverage"]["selected_fraction_settled"])
        with self.assertRaises(ValueError):
            evaluate([row(outcome=None, captured_at="2026-09-19T14:01:00Z")])

    def test_timestamp_order_timezone_equivalence_and_utc_week(self):
        valid = row(captured_at="2026-09-19T22:00:00+08:00")
        self.assertEqual(len(validate([valid])), 1)  # Captured equals decision in UTC.
        week = row(kickoff_at="2026-09-21T00:30:00+08:00",
                   decision_at="2026-09-20T23:30:00+08:00",
                   captured_at="2026-09-20T23:29:00+08:00")
        self.assertEqual(validate([week])[0]["_week"], "2026-W38")  # UTC Sunday.
        for changes in (
            {"decision_at": "2026-09-19T15:00:00Z"},
            {"captured_at": "2026-09-19T14:00:01Z"},
            {"trained_through": "2026-09-19T22:00:00+08:00"},
            {"kickoff_at": "2026-09-19T15:00:00"},
            {"trained_through": "not a timestamp"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate([row(**changes)])

    def test_declared_horizon_must_match_actual_lead_time(self):
        mislabeled = row(decision_at="2026-09-19T14:55:00Z",
                         captured_at="2026-09-19T14:54:00Z")
        with self.assertRaisesRegex(ValueError, "lead time"):
            evaluate([mislabeled])
        self.assertEqual(evaluate([dict(mislabeled, horizon="T-5m")])["scope"]["horizon"], "T-5m")
        almost = row(decision_at="2026-09-19T14:00:30Z", captured_at="2026-09-19T14:00:29Z")
        for tolerance in (0, 0.49):
            with self.subTest(tolerance=tolerance), self.assertRaisesRegex(ValueError, "lead time"):
                evaluate([almost], horizon_tolerance_minutes=tolerance)
        result = evaluate([almost], horizon_tolerance_minutes=0.5)
        self.assertEqual(result["scope"]["horizon_tolerance_minutes"], 0.5)
        for invalid in ("T-0m", "T--60m", "T-60s", "T-060m", "T-1.5m"):
            with self.subTest(horizon=invalid), self.assertRaisesRegex(ValueError, "positive integer"):
                evaluate([row(horizon=invalid)])
        for tolerance in (-1, float("nan"), float("inf"), True):
            with self.subTest(tolerance=tolerance), self.assertRaisesRegex(ValueError, "nonnegative and finite"):
                evaluate([row()], horizon_tolerance_minutes=tolerance)

    def test_invalid_probabilities_metadata_outcomes_and_duplicates(self):
        for probabilities in ([float("nan"), 0, 1], [float("inf"), 0, 0],
                              [0.4, 0.3, 0.2], [-0.1, 0.1, 1], [True, 0, 0], [0.5, 0.5]):
            with self.subTest(probabilities=probabilities), self.assertRaises(ValueError):
                validate([row(p_candidate=probabilities)])
        for changes in ({"selected": 1}, {"outcome": "X"}, {"model_version": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate([row(**changes)])
        missing = row()
        del missing["trained_through"]
        with self.assertRaises(ValueError):
            validate([missing])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate([row(), row(horizon="T-15m")])
        for key in ("league", "horizon", "model_version"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "mixed"):
                validate([row(), row(match_id="m2", **{key: "other"})])

    def test_empty_and_one_week_have_no_interval(self):
        with self.assertRaises(ValueError):
            evaluate([])
        for iterations in (0, -1, True):
            with self.assertRaises(ValueError):
                evaluate([row()], iterations)
        result = evaluate([row()])
        self.assertTrue(result["auditOnly"])
        self.assertFalse(result["automaticPromotion"])
        self.assertEqual(result["paired_bootstrap"]["n_weeks"], 1)
        self.assertIsNone(result["paired_bootstrap"]["rps"]["ci95"])
        self.assertAlmostEqual(result["paired_bootstrap"]["rps"]["difference"], -0.095)

    def test_paired_week_bootstrap_sign_bounds_and_reproducibility(self):
        records = [row(), row(match_id="m2", kickoff_at="2026-09-26T15:00:00Z",
                              decision_at="2026-09-26T14:00:00Z",
                              captured_at="2026-09-26T13:59:00Z")]
        first = evaluate(records, bootstrap=100, seed=19)
        self.assertEqual(first, evaluate(records, bootstrap=100, seed=19))
        bootstrap = first["paired_bootstrap"]
        self.assertEqual(bootstrap["n_weeks"], 2)
        for value in bootstrap["rps"]["ci95"]:
            self.assertAlmostEqual(value, -0.095)
        self.assertEqual(bootstrap["accuracy"]["ci95"], [0, 0])

    def test_bootstrap_resamples_whole_weeks_and_weights_by_matches(self):
        strong = {"p_baseline": [0, 0, 1], "p_candidate": [1, 0, 0]}
        records = [row(**strong), row(match_id="m2", **strong),
                   row(match_id="m3", kickoff_at="2026-09-26T15:00:00Z",
                       decision_at="2026-09-26T14:00:00Z", captured_at="2026-09-26T13:59:00Z",
                       p_baseline=[1, 0, 0], p_candidate=[0, 0, 1])]
        bootstrap = evaluate(records, bootstrap=1, seed=0)["paired_bootstrap"]
        self.assertAlmostEqual(bootstrap["rps"]["difference"], -1 / 3)
        self.assertAlmostEqual(bootstrap["accuracy"]["difference"], 1 / 3)
        # Seed 0 selects the second week twice, keeping its paired differences together.
        self.assertEqual(bootstrap["rps"]["ci95"], [1, 1])
        self.assertEqual(bootstrap["accuracy"]["ci95"], [-1, -1])

    def test_jsonl_and_cli_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "predictions.jsonl"
            output = Path(directory) / "audit.json"
            source.write_text("\n" + json.dumps(row()) + "\n", encoding="utf-8")
            self.assertEqual(len(read_rows(source)), 1)
            command = [sys.executable, str(Path(__file__).with_name("evaluate.py")),
                       str(source), "--output", str(output), "--bootstrap", "10"]
            completed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(json.loads(output.read_text(encoding="utf-8"))["auditOnly"])
            source.write_text(json.dumps(row(decision_at="2026-09-19T14:00:30Z")), encoding="utf-8")
            allowed = subprocess.run(command + ["--horizon-tolerance-minutes", "0.5"],
                                     capture_output=True, text=True)
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["scope"]["horizon_tolerance_minutes"], 0.5)
            source.write_text("\n", encoding="utf-8")
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(failed.returncode, 2)
            self.assertIn("no records", failed.stderr)


if __name__ == "__main__":
    unittest.main()
