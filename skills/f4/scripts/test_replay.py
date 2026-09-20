"""Synthetic, offline regression checks for historical replay invariants.

No live cache or experiment manifest is changed. Cache-loading tests patch only
file reads; end-to-end checks use generated results with fixed split boundaries.
"""
import copy
import json
import math
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np

import replay


def manifest():
    return {
        'experiment_id': 'synthetic-only',
        'leagues': ['synthetic'], 'seasons': ['2425'], 'odds_prefix': 'b365c',
        'warmup_end': '2024-11-01', 'train_end': '2025-01-01',
        'validation_end': '2025-04-01', 'test_end': '2025-06-01',
        'external_start': '2025-08-01',
        'elo_k': 20, 'elo_home_advantage': 60, 'season_retention': 0.75,
        'poisson_prior_games': 8, 'penalties': [1, 10],
        'probability_guard_relative': 0.01,
        'limitations': ['Synthetic fixture; no empirical accuracy claim.'],
        'test_interpretation': 'Synthetic checks only.',
    }


def row(day, home='A', away='B', hg=1, ag=0, market=None, league='synthetic'):
    iso = date.fromisoformat(day).isocalendar()
    return {
        'match_id': '|'.join((league, day, home, away)),
        'date': day, 'league': league, 'season': '2425',
        'home': home, 'away': away, 'hg': hg, 'ag': ag,
        'outcome': 'H' if hg > ag else 'A' if hg < ag else 'D',
        'p_market': [0.45, 0.30, 0.25] if market is None else market,
        '_week': f'{iso.year}-W{iso.week:02d}', 'source_file': 'synthetic',
    }


def change_result(record, hg, ag):
    record.update(hg=hg, ag=ag, outcome='H' if hg > ag else 'A' if hg < ag else 'D')


def raw_row(day='01/08/2024', home='A', away='B', hg=1, ag=0, **updates):
    result = {'date': day, 'home': home, 'away': away,
              'fthg': hg, 'ftag': ag, 'b365c_h': 2.1,
              'b365c_d': 3.2, 'b365c_a': 3.8}
    result.update(updates)
    return result


def load_synthetic(raw_matches):
    serialized = json.dumps({'season': '2425', 'source': 'synthetic',
                             'fetchedAt': '2024-01-01T00:00:00Z',
                             'matches': raw_matches})
    with patch.object(Path, 'read_text', return_value=serialized), \
            patch.object(Path, 'read_bytes', return_value=serialized.encode('utf-8')):
        return replay.load_cache(Path('not-a-real-cache'), manifest())


class TemporalFeatureTests(unittest.TestCase):
    def setUp(self):
        self.config = manifest()

    def assert_feature_equal(self, before, after):
        self.assertEqual(before['features'], after['features'])
        self.assertEqual(before['p_poisson'], after['p_poisson'])
        self.assertEqual(before['history_games_home_role'], after['history_games_home_role'])
        self.assertEqual(before['history_games_away_role'], after['history_games_away_role'])

    def test_entire_day_ignores_its_own_goals_and_outcomes(self):
        rows = [row('2024-08-01'),
                row('2024-08-02', 'A', 'B'),
                row('2024-08-02', 'B', 'C', 0, 0),
                row('2024-08-02', 'A', 'C', 0, 3),
                row('2024-08-03', 'A', 'B')]
        original = copy.deepcopy(rows)
        changed = copy.deepcopy(rows)
        for r in changed:
            if r['date'] == '2024-08-02':
                change_result(r, 0, 12)
        a = replay.construct_features(rows, self.config)
        b = replay.construct_features(changed, self.config)
        self.assertEqual(rows, original, 'Feature construction must not mutate source rows')
        for before, after in zip(a[:-1], b[:-1]):
            self.assert_feature_equal(before, after)
        self.assertNotEqual(a[-1]['p_poisson'], b[-1]['p_poisson'])
        self.assertNotEqual(a[-1]['features'], b[-1]['features'])

    def test_same_day_order_cannot_change_features_or_next_day_state(self):
        rows = [row('2024-08-01'), row('2024-08-02', 'A', 'B', 3, 0),
                row('2024-08-02', 'B', 'C', 0, 2),
                row('2024-08-02', 'A', 'C', 1, 1), row('2024-08-03')]
        reordered = rows[:1] + list(reversed(rows[1:4])) + rows[4:]
        before = {r['match_id']: r for r in replay.construct_features(rows, self.config)}
        after = {r['match_id']: r for r in replay.construct_features(reordered, self.config)}
        for match_id in before:
            self.assert_feature_equal(before[match_id], after[match_id])

    def test_future_goals_do_not_change_earlier_features_or_frozen_predictions(self):
        rows = [row('2024-08-01'), row('2024-08-02', hg=0, ag=0),
                row('2024-08-03', hg=0, ag=2), row('2024-08-04')]
        changed = copy.deepcopy(rows)
        change_result(changed[-1], 20, 0)
        a = replay.construct_features(rows, self.config)
        b = replay.construct_features(changed, self.config)
        coefficients = np.array([[0.4, -0.1, -0.3], [0.2, 0.05, -0.25]])
        for before, after in zip(a, b):
            self.assert_feature_equal(before, after)
        for offset in (False, True):
            self.assertEqual(replay.predict_logistic(a, coefficients, offset),
                             replay.predict_logistic(b, coefficients, offset))

    def test_elo_changes_only_on_following_date_by_expected_amount(self):
        features = replay.construct_features(
            [row('2024-08-01'), row('2024-08-02')], self.config)
        initial = self.config['elo_home_advantage'] / 400
        expected = 1 / (1 + 10 ** -initial)
        next_difference = (self.config['elo_home_advantage']
                           + 2 * self.config['elo_k'] * (1 - expected)) / 400
        self.assertAlmostEqual(features[0]['features'][0], initial, places=14)
        self.assertAlmostEqual(features[1]['features'][0], next_difference, places=14)
        self.assertEqual(features[0]['history_games_home_role'], 0)
        self.assertEqual(features[1]['history_games_home_role'], 1)

    def test_cold_start_and_scoreless_history_produce_positive_priors(self):
        features = replay.construct_features(
            [row('2024-08-01', hg=0, ag=0), row('2024-08-02', hg=0, ag=0),
             row('2024-08-03', hg=0, ag=0)], self.config)
        self.assertGreater(features[0]['p_poisson'][0], features[0]['p_poisson'][2])
        for r in features:
            self.assertAlmostEqual(sum(r['p_poisson']), 1.0, places=14)
            self.assertTrue(all(math.isfinite(p) and 0 < p < 1 for p in r['p_poisson']))

    def test_poisson_home_away_symmetry(self):
        for home, away in ((1.5, 1.2), (0.1, 3.0), (4.0, 4.0)):
            p = replay.poisson_probs(home, away)
            reverse = replay.poisson_probs(away, home)
            self.assertAlmostEqual(sum(p), 1.0, places=14)
            self.assertTrue(all(0 < value < 1 for value in p))
            np.testing.assert_allclose(p, reverse[::-1], rtol=1e-13, atol=1e-15)


class CacheAndSplitTests(unittest.TestCase):
    def test_invalid_odds_keep_result_history_but_not_market_comparison(self):
        for bad_value in (None, 'bad', 1.0, 0, float('inf'), float('nan')):
            with self.subTest(odds=bad_value):
                rows, exclusions, sources = load_synthetic([
                    raw_row(b365c_h=bad_value), raw_row(day='02/08/2024')])
                self.assertEqual(len(rows), 2)
                self.assertIsNone(rows[0]['p_market'])
                self.assertEqual([r['stage'] for r in exclusions], ['odds'])
                self.assertEqual(sources[0]['raw_rows'], 2)
                featured = replay.construct_features(rows, manifest())
                self.assertEqual(featured[1]['history_games_home_role'], 1)
                self.assertEqual(len([r for r in featured if r['p_market'] is not None]), 1)

    def test_missing_odds_keep_result_and_valid_odds_devig(self):
        missing = raw_row()
        del missing['b365c_d']
        rows, exclusions, _ = load_synthetic([missing, raw_row(day='02/08/2024')])
        self.assertIsNone(rows[0]['p_market'])
        self.assertEqual(exclusions[0]['stage'], 'odds')
        inverses = np.array([1 / 2.1, 1 / 3.2, 1 / 3.8])
        np.testing.assert_allclose(rows[1]['p_market'], inverses / inverses.sum())

    def test_invalid_result_is_excluded_including_infinite_goal(self):
        for bad_goal in (-1, True, 1.5, None, float('nan'), float('inf')):
            with self.subTest(goal=bad_goal):
                rows, exclusions, _ = load_synthetic([raw_row(hg=bad_goal)])
                self.assertEqual(rows, [])
                self.assertEqual(len(exclusions), 1)
                self.assertEqual(exclusions[0]['stage'], 'result')

    def test_duplicate_identity_with_conflicting_result_or_odds_fails(self):
        for changed in ({'fthg': 4}, {'b365c_h': 2.9}):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, 'Conflicting duplicate'):
                    load_synthetic([raw_row(), raw_row(**changed)])

    def test_identical_duplicates_excluded_and_rows_sorted(self):
        first = raw_row()
        rows, exclusions, _ = load_synthetic([raw_row(day='02/08/2024'), first, first.copy()])
        self.assertEqual([r['date'] for r in rows], ['2024-08-01', '2024-08-02'])
        self.assertEqual(exclusions[0]['reason'], 'duplicate')
        self.assertEqual(len(rows), 2)

    def test_split_boundaries_are_half_open_with_explicit_unused_gap(self):
        expected = {
            '2024-10-31': 'warmup', '2024-11-01': 'train',
            '2024-12-31': 'train', '2025-01-01': 'validation',
            '2025-03-31': 'validation', '2025-04-01': 'test',
            '2025-05-31': 'test', '2025-06-01': 'unused',
            '2025-07-31': 'unused', '2025-08-01': 'external_partial',
        }
        for day, split in expected.items():
            self.assertEqual(replay.split_name(day, manifest()), split)


class LogisticAndSelectionTests(unittest.TestCase):
    def test_balanced_intercept_model_has_uniform_probabilities(self):
        rows = [dict(features=[1.0], outcome=y, p_market=[0.6, 0.3, 0.1])
                for y in 'HDA' * 4]
        coefficients, details = replay.fit_logistic(rows, penalty=1, offset=False)
        np.testing.assert_allclose(coefficients, 0, atol=1e-10)
        np.testing.assert_allclose(replay.predict_logistic(rows, coefficients, False),
                                   np.full((len(rows), 3), 1 / 3), atol=1e-12)
        self.assertFalse(details['market_offset'])

    def test_calibrated_market_offset_requires_no_adjustment(self):
        rows = [dict(features=[1.0], outcome=y, p_market=[0.6, 0.3, 0.1])
                for y in 'HHHHHHDDDA']
        coefficients, details = replay.fit_logistic(rows, penalty=10, offset=True)
        np.testing.assert_allclose(coefficients, 0, atol=1e-10)
        predictions = replay.predict_logistic(rows, coefficients, True)
        np.testing.assert_allclose(predictions, [r['p_market'] for r in rows], atol=1e-12)
        self.assertTrue(details['market_offset'])

    def test_elo_logistic_learns_direction_and_returns_finite_simplex(self):
        rows = [dict(features=[x, 1.0], outcome=y, p_market=[0.4, 0.3, 0.3])
                for x, y in [(-2, 'A'), (-1, 'A'), (0, 'D'), (1, 'H'), (2, 'H')] * 5]
        coefficients, _ = replay.fit_logistic(rows, penalty=1, offset=False)
        predictions = replay.predict_logistic(rows[:5], coefficients, False)
        self.assertGreater(predictions[-1][0], predictions[0][0])
        self.assertGreater(predictions[0][2], predictions[-1][2])
        self.assertEqual([int(np.argmax(p)) for p in predictions], [2, 2, 1, 0, 0])
        for p in predictions:
            self.assertAlmostEqual(sum(p), 1.0, places=14)
            self.assertTrue(all(math.isfinite(v) and 0 < v < 1 for v in p))

    def test_selection_rejects_violating_any_single_probability_guard(self):
        baseline = dict(accuracy=0.50, log_loss=1.0, rps=0.20, brier=0.60)
        for key in ('log_loss', 'rps', 'brier'):
            values = dict(baseline, accuracy=0.99)
            values[key] = baseline[key] * 1.0101
            with self.subTest(metric=key):
                self.assertEqual(replay.select_candidate({'market': baseline, 'bad': values}, 0.01),
                                 'market')

    def test_selection_guard_boundary_is_inclusive(self):
        baseline = dict(accuracy=0.50, log_loss=1.0, rps=0.20, brier=0.60)
        candidate = {key: value * 1.01 for key, value in baseline.items()}
        candidate['accuracy'] = 0.60
        self.assertEqual(replay.select_candidate({'market': baseline, 'edge': candidate}, 0.01), 'edge')

    def test_selection_ties_lower_logloss_then_market_then_stable_name(self):
        baseline = dict(accuracy=0.50, log_loss=1.0, rps=0.20, brier=0.60)
        better_loss = dict(baseline, log_loss=0.9)
        self.assertEqual(replay.select_candidate({'market': baseline, 'lower_loss': better_loss}, 0.01),
                         'lower_loss')
        self.assertEqual(replay.select_candidate({'aaa': baseline.copy(), 'market': baseline}, 0.01),
                         'market')
        self.assertEqual(replay.select_candidate({'zzz': better_loss, 'market': baseline,
                                                  'aaa': better_loss.copy()}, 0.01), 'aaa')


def experiment_rows():
    rows = []
    for start, count in (('2024-10-01', 8), ('2024-11-01', 28),
                         ('2025-01-01', 20), ('2025-04-01', 20), ('2025-08-01', 16)):
        for i in range(count):
            day = (date.fromisoformat(start) + timedelta(days=i)).isoformat()
            home, away = ('A', 'B') if i % 2 == 0 else ('B', 'A')
            hg, ag = ((2, 0), (0, 0), (1, 2), (3, 1))[i % 4]
            probabilities = ([0.55, 0.25, 0.20] if i % 2 == 0 else [0.30, 0.30, 0.40])
            rows.append(row(day, home, away, hg, ag, probabilities))
    rows[2]['p_market'] = None
    return rows


class SyntheticBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = experiment_rows()
        cls.config = manifest()
        with patch.object(replay, 'select_candidate', wraps=replay.select_candidate) as select:
            cls.summary, cls.predictions = replay.benchmark(cls.rows, cls.config)
        cls.selection_input = select.call_args.args[0]
        changed = copy.deepcopy(cls.rows)
        for record in changed:
            if replay.split_name(record['date'], cls.config) in ('test', 'external_partial'):
                change_result(record, 0 if record['outcome'] == 'H' else 9,
                              9 if record['outcome'] == 'H' else 0)
        cls.changed_summary, cls.changed_predictions = replay.benchmark(changed, cls.config)

    def test_benchmark_uses_validation_metrics_for_selection(self):
        self.assertEqual(self.selection_input, self.summary['metrics']['validation'])
        self.assertNotEqual(self.selection_input, self.summary['metrics']['test'])
        self.assertEqual(self.summary['selected_on_validation'], replay.select_candidate(
            self.summary['metrics']['validation'], self.config['probability_guard_relative']))

    def test_test_outcome_changes_cannot_change_training_or_selected_model(self):
        self.assertEqual(self.summary['fitted_models'], self.changed_summary['fitted_models'])
        self.assertEqual(self.summary['selected_on_validation'],
                         self.changed_summary['selected_on_validation'])
        self.assertEqual(self.summary['metrics']['validation'],
                         self.changed_summary['metrics']['validation'])
        self.assertEqual(self.summary['metrics']['train'], self.changed_summary['metrics']['train'])
        self.assertNotEqual(self.summary['metrics']['test'], self.changed_summary['metrics']['test'])
        for before, after in zip(self.predictions, self.changed_predictions):
            if before['date'] <= self.config['validation_end']:
                self.assertEqual(before['features'], after['features'])
                self.assertEqual(before['predictions'], after['predictions'])

    def test_complete_synthetic_experiment_has_consistent_common_sample_and_probabilities(self):
        self.assertEqual(self.summary['split_counts'],
                         dict(warmup=7, train=28, validation=20, test=20,
                              external_partial=16, unused=0))
        self.assertEqual(self.summary['coverage']['valid_result_rows'], 92)
        self.assertEqual(self.summary['coverage']['comparable_quote_rows'], 91)
        for split, models in self.summary['metrics'].items():
            for scores in models.values():
                self.assertEqual(scores['n'], self.summary['split_counts'][split])
        self.assertEqual(len(self.summary['fitted_models']), 4)
        for record in self.predictions:
            self.assertEqual(len(record['predictions']), 6)
            for p in record['predictions'].values():
                self.assertAlmostEqual(sum(p), 1.0, places=13)
                self.assertTrue(all(math.isfinite(v) and 0 < v < 1 for v in p))
        for split in ('test', 'external_partial'):
            market_difference = self.summary['paired_differences_vs_market'][split]['market']
            self.assertEqual(market_difference['rps']['difference'], 0.0)
            self.assertEqual(market_difference['accuracy']['difference'], 0.0)
        self.assertTrue(self.summary['historicalReplay'])
        self.assertFalse(self.summary['prospectiveEvidence'])
        self.assertFalse(self.summary['automaticPromotion'])


if __name__ == '__main__':
    unittest.main()
