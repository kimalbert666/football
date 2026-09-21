"""Offline end-to-end checks; synthetic fixtures never enter production data."""
import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import f4_cloud as cloud


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
        self.kickoff = self.now + timedelta(hours=3)
        self.f2_calls = 0
        cloud.write_json(self.root / 'data/f4/config.json', {
            'mode': 'shadow', 'candidate_weight': 0, 'experiment_id': 'synthetic-only',
            'league': 'england-premier', 'tracked_teams_path': 'teams.json',
            'horizons_minutes': [180, 60], 'horizon_tolerance_minutes': 20, 'max_capture_age_minutes': 15,
        })
        cloud.write_json(self.root / 'teams.json', {'teams': [{'canonicalId': 'arsenal'}]})
        self.fixture = {'match_id': 'espn:synthetic', 'source_event_id': 'synthetic', 'provider': 'espn',
                        'league': 'england-premier', 'home_id': 'arsenal', 'away_id': 'brighton',
                        'home': 'Arsenal', 'away': 'Brighton', 'kickoff_at': cloud.stamp(self.kickoff),
                        'status': 'scheduled', 'score': None, 'problems': [],
                        'market': {'odds': [2.0, 3.2, 4.0], 'captured_at': cloud.stamp(self.now),
                                   'published_at': None, 'source': 'synthetic'}}

    def collector(self, root, now, **kwargs):
        return {'captured_at': cloud.stamp(self.now), 'fixtures': [copy.deepcopy(self.fixture)],
                'sources': [], 'coverage': {'complete': True}, 'problems': []}

    def candidate(self, root, fixture, decision):
        return {'p_candidate': [0.6, 0.25, 0.15], 'candidate_version': 'synthetic-v1',
                'trained_through': '2026-09-01T00:00:00Z', 'training_evidence': 'synthetic',
                'source_sha256': 'a' * 64, 'reason': None}

    def f2(self, root, fixtures):
        self.f2_calls += 1
        return {'sources': [], 'predictions': {}, 'problems': []}

    def run_cloud(self, command):
        return cloud.run(self.root, command, collector=self.collector, candidate=self.candidate,
                         f2_fetcher=self.f2, clock=lambda: self.now)

    def test_capture_idempotency_preserves_first_and_does_not_repeat_f2(self):
        first = self.run_cloud('capture')
        self.assertEqual(first['new_valid_predictions'], 1)
        original = cloud.load_predictions(self.root)
        self.now += timedelta(minutes=1)
        second = self.run_cloud('capture')
        self.assertEqual(second['new_predictions'], 0)
        self.assertEqual(self.f2_calls, 1)
        self.assertEqual(original, cloud.load_predictions(self.root))
        row = original[0]
        self.assertEqual(row['p_champion'], row['p_baseline'])
        self.assertFalse(row['selected'])
        self.assertEqual(row['candidate_weight'], 0)

    def test_missing_market_can_retry_without_overwriting_old_snapshot(self):
        market = self.fixture['market']
        self.fixture['market'] = None
        self.run_cloud('capture')
        old = cloud.load_predictions(self.root)[0]
        self.now += timedelta(minutes=1)
        market['captured_at'] = cloud.stamp(self.now)
        self.fixture['market'] = market
        self.run_cloud('capture')
        rows = cloud.load_predictions(self.root)
        self.assertEqual(len(rows), 2)
        self.assertIn(old, rows)
        self.assertEqual(sum(r['p_baseline'] is not None for r in rows), 1)

    def test_source_delay_past_kickoff_cannot_create_prediction(self):
        def delayed(root, now, **kwargs):
            self.now = self.kickoff + timedelta(seconds=1)
            return self.collector(root, now, **kwargs)
        result = cloud.run(self.root, 'capture', collector=delayed, candidate=self.candidate,
                           f2_fetcher=self.f2, clock=lambda: self.now)
        self.assertEqual(result['new_predictions'], 0)
        self.assertEqual(self.f2_calls, 0)
        self.assertEqual(cloud.load_predictions(self.root), [])

    def test_outcome_and_correction_never_change_prediction(self):
        self.run_cloud('capture')
        original = cloud.load_predictions(self.root)
        self.now = self.kickoff + timedelta(hours=3)
        self.fixture.update(status='completed', score=[1, 0], market=None)
        result = self.run_cloud('daily')
        self.assertEqual(result['new_outcomes'], 1)
        self.now += timedelta(minutes=1)
        self.assertEqual(self.run_cloud('daily')['new_outcomes'], 0)
        self.fixture['score'] = [0, 1]
        self.now += timedelta(minutes=1)
        self.assertEqual(self.run_cloud('daily')['new_outcomes'], 1)
        self.assertEqual(original, cloud.load_predictions(self.root))
        self.assertEqual(len(list((self.root / 'data/f4/outcomes').rglob('*.json'))), 2)

    def test_unresolved_fixture_does_not_crash_or_invent_probabilities(self):
        self.fixture.update(kickoff_at=None, away_id=None)
        result = self.run_cloud('capture')
        self.assertEqual(result['new_predictions'], 0)
        self.assertEqual(cloud.load_predictions(self.root), [])

    def test_market_rejects_future_capture_or_update_and_invalid_odds(self):
        config = cloud.read_json(self.root / 'data/f4/config.json', {})
        for field in ('captured_at', 'published_at'):
            row = copy.deepcopy(self.fixture)
            row['market'][field] = cloud.stamp(self.now + timedelta(seconds=1))
            self.assertIsNone(cloud.market_probability(row, self.now, config)[0])

    def test_crown_experiment_never_accepts_sporttery_as_crown(self):
        config = cloud.read_json(self.root / 'data/f4/config.json', {})
        config['required_bookmaker'] = 'crown'
        self.assertIsNone(cloud.market_probability(self.fixture, self.now, config)[0])
        self.fixture['market']['bookmaker'] = 'crown'
        self.assertIsNotNone(cloud.market_probability(self.fixture, self.now, config)[0])
        for odds in ([True, 3, 4], [1, 3, 4], [float('nan'), 3, 4], [2, 3]):
            row = copy.deepcopy(self.fixture)
            row['market']['odds'] = odds
            self.assertIsNone(cloud.market_probability(row, self.now, config)[0])

    def test_future_training_cutoff_excludes_candidate_preserves_baseline(self):
        original = self.candidate
        self.candidate = lambda *args: {**original(*args), 'trained_through': cloud.stamp(self.now)}
        result = self.run_cloud('capture')
        self.assertEqual(result['new_valid_predictions'], 1)
        row = cloud.load_predictions(self.root)[0]
        self.assertIsNone(row['p_candidate'])
        self.assertIsNone(row['trained_through'])
        self.assertEqual(row['rejected_candidate_cutoff'], cloud.stamp(self.now))

    def test_league_scope_includes_teams_outside_old_four_team_list(self):
        config = cloud.read_json(self.root / 'data/f4/config.json', {})
        config['scope'] = 'league'
        cloud.write_json(self.root / 'data/f4/config.json', config)
        cloud.write_json(self.root / 'teams.json', {'teams': []})
        self.fixture.update(home_id='fulham', away_id='chelsea', home='Fulham', away='Chelsea')
        self.assertEqual(self.run_cloud('capture')['new_valid_predictions'], 1)
        self.assertEqual(cloud.load_predictions(self.root)[0]['home_id'], 'fulham')

    def test_f2_point_forecast_is_scored_as_direction_only(self):
        point = {'score': [2, 1], 'direction': 'H', 'captured_at': cloud.stamp(self.now),
                 'published_at': None, 'event_kickoff_at': cloud.stamp(self.kickoff),
                 'home_id': 'arsenal', 'away_id': 'brighton'}
        self.f2 = lambda *args: {'sources': [], 'problems': [],
                                'predictions': {self.fixture['match_id']: point}}
        self.run_cloud('capture')
        self.now = self.kickoff + timedelta(hours=3)
        self.fixture.update(status='completed', score=[3, 0], market=None)
        self.run_cloud('daily')
        summary = cloud.read_json(self.root / 'data/f4/evaluations/latest.json', {})
        self.assertEqual(summary['groups'][0]['f2']['n'], 1)
        self.assertEqual(summary['groups'][0]['f2']['accuracy'], 1)
        self.assertNotIn('log_loss', summary['groups'][0]['f2'])

    def test_unchanged_review_has_stable_notification_marker(self):
        self.run_cloud('all')
        first = cloud.read_json(self.root / '.github/run-logs/f4-notify-token.json', {})
        cloud.write_json(self.root / 'data/f4/status/notification.json', first)
        self.now += timedelta(seconds=1)
        self.run_cloud('review')
        second = cloud.read_json(self.root / '.github/run-logs/f4-notify-token.json', {})
        self.assertEqual(first, second)
        self.now += timedelta(seconds=1)
        with patch.object(cloud, 'outputs') as emit:
            self.run_cloud('capture')
        self.assertFalse(emit.call_args.kwargs['notify'])

    def test_failed_notification_retries_after_records_are_already_saved(self):
        self.run_cloud('all')
        first = cloud.read_json(self.root / '.github/run-logs/f4-notify-token.json', {})
        self.now += timedelta(seconds=1)
        with patch.object(cloud, 'outputs') as emit:
            result = self.run_cloud('capture')
        self.assertEqual(result['new_predictions'], 0)
        self.assertTrue(emit.call_args.kwargs['notify'])
        self.assertEqual(first, cloud.read_json(self.root / '.github/run-logs/f4-notify-token.json', {}))


if __name__ == '__main__':
    unittest.main()
