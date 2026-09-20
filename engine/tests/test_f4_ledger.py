"""Synthetic observations only: immutability, time safety and fair pairing."""
import copy
import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from f4_ledger import save_prediction, load_predictions, save_outcome, load_latest_outcomes, evaluate_ledger, render_report


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.row = {"match_id": "synthetic:1", "league": "england-premier", "home_id": "arsenal", "away_id": "brighton",
                    "experiment_id": "synthetic-only", "kickoff_at": "2026-10-01T15:00:00Z", "horizon": "T-180m",
                    "horizon_tolerance_minutes": 20, "captured_at": "2026-10-01T11:59:00Z",
                    "decision_at": "2026-10-01T12:00:00Z", "generated_at": "2026-10-01T12:00:01Z",
                    "p_baseline": [.5, .3, .2], "p_candidate": [.4, .35, .25], "p_champion": [.5, .3, .2],
                    "candidate_version": "frozen-v1", "selected": False, "trained_through": "2026-09-01T00:00:00Z",
                    "candidate_training_evidence": "declared_only_unverified"}
        self.now = datetime(2026, 10, 1, 18, tzinfo=timezone.utc)

    def outcome(self, **changes):
        row = {key: self.row[key] for key in ("match_id", "league", "home_id", "away_id", "kickoff_at")}
        return {**row, "status": "completed", "home_score": 2, "away_score": 0, **changes}

    def later(self, **changes):
        return {**self.row, "captured_at": "2026-10-01T12:01:00Z", "decision_at": "2026-10-01T12:01:00Z",
                "generated_at": "2026-10-01T12:01:01Z", **changes}

    def write_fixture(self, row, name="fixture"):
        path = self.root / "data/f4/fixtures" / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row), encoding="utf-8")

    def test_prediction_immutable_idempotent_normalizes_timezone(self):
        path, created = save_prediction(self.root, self.row)
        original = path.read_bytes()
        self.assertTrue(created)
        equivalent = {**self.row, "kickoff_at": "2026-10-01T23:00:00+08:00"}
        self.assertEqual(save_prediction(self.root, equivalent), (path, False))
        with self.assertRaises(ValueError):
            save_prediction(self.root, {**self.row, "p_candidate": [.6, .2, .2]})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.parent.name, "2026-10-01")

    def test_rejects_bad_probabilities_identity_time_and_selection(self):
        invalid = [{"p_baseline": [float("nan"), .5, .5]}, {"p_baseline": [float("inf"), 0, 0]},
                   {"p_baseline": [.5, .2, .2]}, {"p_candidate": [True, 0, 0]}, {"selected": 0},
                   {"home_id": "brighton"}, {"match_id": ""}, {"decision_at": "2026-10-01T12:00:00"},
                   {"captured_at": "2026-10-01T12:01:00Z"}, {"generated_at": "2026-10-01T15:00:00Z"},
                   {"trained_through": self.row["decision_at"]}, {"horizon": "T-60m"},
                   {"horizon_tolerance_minutes": 21}, {"actual_minutes": 179}, {"p_f2": [.6, .2, .2]}]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                save_prediction(self.root, {**self.row, **change})
        self.assertEqual(load_predictions(self.root), [])

    def test_missing_baseline_retry_but_candidate_locked_to_first_valid(self):
        save_prediction(self.root, {**self.row, "p_baseline": None, "p_champion": None})
        first = self.later(p_candidate=None)
        save_prediction(self.root, first)
        save_prediction(self.root, {**self.later(), "decision_at": "2026-10-01T12:02:00Z", "generated_at": "2026-10-01T12:02:01Z"})
        save_outcome(self.root, self.outcome(), self.now)
        summary = evaluate_ledger(self.root, self.now)
        self.assertEqual(summary["counts"]["raw_snapshots"], 3)
        self.assertEqual(summary["counts"]["effective_valid"], 1)
        self.assertEqual(summary["counts"]["missing_baseline"], 1)
        self.assertEqual(summary["baseline"]["n"], 1)
        self.assertEqual(summary["candidate"]["n"], 0)
        self.assertEqual(summary["paired_baseline"]["n"], 0)
        self.assertIsNone(summary["candidate"]["accuracy"])

    def test_candidate_and_baseline_metrics_use_identical_subset(self):
        save_prediction(self.root, self.row)
        save_outcome(self.root, self.outcome(), self.now)
        save_prediction(self.root, {**self.row, "match_id": "synthetic:2", "p_candidate": None})
        save_outcome(self.root, self.outcome(match_id="synthetic:2", home_score=0, away_score=1), self.now)
        summary = evaluate_ledger(self.root, self.now)
        self.assertEqual(summary["baseline"]["n"], 2)
        self.assertEqual(summary["baseline"]["accuracy"], .5)
        self.assertEqual(summary["paired_baseline"]["n"], 1)
        self.assertEqual(summary["candidate"]["n"], 1)
        self.assertEqual(summary["paired_baseline"]["accuracy"], 1)
        self.assertAlmostEqual(summary["paired_baseline"]["rps"], (.5 ** 2 + .2 ** 2) / 2)
        self.assertAlmostEqual(summary["paired_baseline"]["brier"], .5 ** 2 + .3 ** 2 + .2 ** 2)
        self.assertAlmostEqual(summary["paired_baseline"]["log_loss"], -math.log(.5))

    def test_pending_missing_samples_are_not_zero_percent(self):
        save_prediction(self.root, self.row)
        summary = evaluate_ledger(self.root, self.now)
        self.assertEqual(summary["counts"]["pending"], 1)
        self.assertIsNone(summary["baseline"]["accuracy"])
        report = render_report(summary)
        self.assertIn("无已结算样本", report)
        self.assertIn("训练资料尚未独立认证", report)
        self.assertFalse(summary["automatic_promotion"])
        self.assertEqual(summary["candidate_weight"], 0)

    def test_outcome_correction_append_only_and_asof_observation_time(self):
        path, _ = save_prediction(self.root, self.row)
        before = path.read_bytes()
        self.assertTrue(save_outcome(self.root, self.outcome(), self.now))
        self.assertFalse(save_outcome(self.root, self.outcome(source="second source"), self.now + timedelta(minutes=1)))
        self.assertTrue(save_outcome(self.root, self.outcome(home_score=0, away_score=1), self.now + timedelta(minutes=2)))
        self.assertEqual(len(list((self.root / "data/f4/outcomes").rglob("*.json"))), 2)
        self.assertEqual(load_latest_outcomes(self.root)[self.row["match_id"]]["away_score"], 1)
        self.assertEqual(evaluate_ledger(self.root, self.now)["baseline"]["accuracy"], 1)
        self.assertEqual(evaluate_ledger(self.root, self.now + timedelta(minutes=2))["baseline"]["accuracy"], 0)
        self.assertEqual(path.read_bytes(), before)

    def test_outcome_rejects_wrong_sides_invalid_scores_and_early_completion(self):
        save_prediction(self.root, self.row)
        for changes in ({"home_id": "chelsea"}, {"home_score": True}, {"home_score": -1}, {"away_score": 1.0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                save_outcome(self.root, self.outcome(**changes), self.now)
        with self.assertRaises(ValueError):
            save_outcome(self.root, self.outcome(), self.row["kickoff_at"])

    def test_postponement_new_kickoff_excludes_old_snapshot(self):
        save_prediction(self.root, self.row)
        save_outcome(self.root, self.outcome(status="scheduled", kickoff_at="2026-10-02T15:00:00Z"), self.now)
        summary = evaluate_ledger(self.root, self.now)
        self.assertEqual(summary["counts"]["excluded_rescheduled"], 1)
        self.assertEqual(summary["baseline"]["n"], 0)
        self.assertEqual(summary["counts"]["pending"], 0)

    def test_cancelled_postponed_live_unknown_never_settle(self):
        for index, status in enumerate(("cancelled", "postponed", "live", "scheduled", "unknown")):
            match_id = "synthetic:" + str(index)
            save_prediction(self.root, {**self.row, "match_id": match_id})
            save_outcome(self.root, self.outcome(match_id=match_id, status=status), self.now)
        summary = evaluate_ledger(self.root, self.now)
        self.assertEqual(summary["baseline"]["n"], 0)
        self.assertEqual(summary["counts"]["excluded_cancelled"], 1)
        self.assertEqual(summary["counts"]["excluded_postponed"], 1)
        self.assertEqual(summary["counts"]["pending"], 3)

    def test_extra_time_and_penalties_require_explicit_ninety_minute_score(self):
        save_prediction(self.root, self.row)
        save_outcome(self.root, self.outcome(status="AET", home_score=4, away_score=2), self.now)
        self.assertEqual(evaluate_ledger(self.root, self.now)["baseline"]["n"], 0)
        save_outcome(self.root, self.outcome(status="PEN", home_score=5, away_score=4, score90=[1, 1]), self.now + timedelta(minutes=1))
        latest = load_latest_outcomes(self.root)[self.row["match_id"]]
        self.assertEqual((latest["home_score"], latest["away_score"]), (1, 1))
        self.assertEqual(evaluate_ledger(self.root, self.now + timedelta(minutes=1))["baseline"]["accuracy"], 0)

    def test_horizons_and_versions_remain_distinct_with_deterministic_tie(self):
        save_prediction(self.root, {**self.row, "p_baseline": [.4, .4, .2]})
        save_prediction(self.root, {**self.row, "horizon": "T-60m", "captured_at": "2026-10-01T14:00:00Z",
                                    "decision_at": "2026-10-01T14:00:00Z", "generated_at": "2026-10-01T14:00:01Z",
                                    "candidate_version": "frozen-v2"})
        save_outcome(self.root, self.outcome(), self.now)
        summary = evaluate_ledger(self.root, self.now)
        self.assertEqual(len(summary["groups"]), 2)
        self.assertEqual(summary["counts"]["effective_valid"], 2)
        self.assertEqual(summary["counts"]["distinct_match_count"], 1)
        self.assertEqual(summary["baseline"]["accuracy"], 1)

    def test_coverage_counts_known_closed_and_late_discovered_separately(self):
        known = {**self.outcome(status="scheduled"), "first_seen_at": "2026-10-01T10:00:00Z", "last_seen_at": "2026-10-01T10:00:00Z"}
        self.write_fixture(known)
        self.write_fixture({**known, "match_id": "synthetic:late", "first_seen_at": "2026-10-01T16:00:00Z", "last_seen_at": "2026-10-01T16:00:00Z"}, "late")
        self.write_fixture({**known, "match_id": "synthetic:unresolved", "away_id": None, "kickoff_at": None}, "unresolved")
        save_prediction(self.root, self.row)
        summary = evaluate_ledger(self.root, self.now)
        coverage = summary["coverage"]
        self.assertEqual(coverage["known_tracked_fixtures"], 3)
        self.assertEqual(coverage["closed_windows"], 2)
        self.assertEqual(coverage["covered_windows"], 1)
        self.assertEqual(coverage["missing_windows"], 1)
        self.assertEqual(coverage["late_discovered_windows"], 2)
        self.assertEqual(coverage["unresolved_fixtures"], 1)

    def test_coverage_rescheduled_registry_ignores_old_kickoff(self):
        save_prediction(self.root, self.row)
        known = {**self.outcome(status="scheduled"), "first_seen_at": "2026-10-01T10:00:00Z", "last_seen_at": "2026-10-01T10:00:00Z"}
        self.write_fixture(known)
        save_outcome(self.root, self.outcome(status="scheduled", kickoff_at="2026-10-02T15:00:00Z"), self.now)
        summary = evaluate_ledger(self.root, self.now + timedelta(days=1))
        self.assertEqual(summary["coverage"]["covered_windows"], 0)
        self.assertEqual(summary["coverage"]["missing_windows"], 2)


if __name__ == "__main__":
    unittest.main()
