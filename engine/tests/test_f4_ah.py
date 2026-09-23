"""All data here are synthetic and stored only in temporary directories."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import f4_ah as ah
import f4_cloud as cloud
from f4_ledger import save_outcome


class AHIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 2, 10, 45, tzinfo=timezone.utc)
        self.fixture = {'match_id': 'espn:test-only', 'provider': 'espn', 'league': 'england-premier',
                        'home_id': 'arsenal', 'away_id': 'chelsea', 'home': 'Arsenal', 'away': 'Chelsea',
                        'kickoff_at': '2026-10-02T11:00:00Z', 'status': 'scheduled'}
        self.cfg = {'enabled': True, 'experiment_id': 'synthetic-ah-v1', 'research_only': True,
                    'automatic_promotion': False, 'automatic_wagering': False,
                    'windows': {'180': 20, '60': 10, '15': 5}, 'max_capture_age_minutes': 5}
        self.config = {'asian_handicap': self.cfg, 'notifications': {
            'policy': 'monthly_and_critical_only', 'started_at': '2026-09-23T00:00:00+08:00'}}
        ah.write(self.root / 'data/f4/config.json', self.config)
        self.quote = {'bookmaker': 'crown', 'market': '90min_asian_handicap', 'home_handicap': -2.75,
                      'odds': [1.9, 1.95], 'captured_at': ah.stamp(self.now), 'published_at': ah.stamp(self.now),
                      'provider_event_id': 'test-only', 'url': 'https://example.invalid/synthetic',
                      'content_sha256': 'a' * 64}

    def source(self, root, fixtures, now, **kwargs):
        return {'markets': {self.fixture['match_id']: copy.deepcopy(self.quote)}, 'sources': [], 'problems': []}

    def run_capture(self, source=None):
        return ah.run_ah(self.root, [self.fixture], 'capture', clock=lambda: self.now, source=source or self.source)

    def test_all_quarter_lines_are_validated_without_examples_whitelist(self):
        for line in (-7.75, -3.5, -2, -0.75, 0, 0.25, 2.75, 9.5):
            with self.subTest(line=line):
                row = {**self.quote, 'home_handicap': line}
                self.assertEqual(ah.validate_quote(row, self.fixture, self.now, 5)['home_handicap'], line)
        for line in (True, float('nan'), 0.3):
            with self.assertRaises(ValueError):
                ah.validate_quote({**self.quote, 'home_handicap': line}, self.fixture, self.now, 5)

    def test_stale_quote_and_late_record_rejected(self):
        with self.assertRaises(ValueError):
            ah.validate_quote(self.quote, self.fixture, self.now + timedelta(minutes=6), 5)
        self.now = ah.instant(self.fixture['kickoff_at'])
        self.assertEqual(self.run_capture()['new_valid'], 0)

    def test_first_valid_snapshot_is_immutable_and_missing_can_retry(self):
        missing = lambda *a, **k: {'markets': {}, 'sources': [], 'problems': ['synthetic missing quote']}
        self.assertEqual(self.run_capture(missing)['new_attempts'], 1)
        first = ah.load_observations(self.root)
        self.now += timedelta(minutes=1)
        self.assertEqual(self.run_capture()['new_valid'], 1)
        self.now += timedelta(minutes=1)
        self.assertEqual(self.run_capture()['new_attempts'], 0)
        self.assertEqual(len(ah.load_observations(self.root)), 2)
        self.assertIn(first[0], ah.load_observations(self.root))

    def test_source_finishing_after_window_never_fakes_timing(self):
        def delayed(*args, **kwargs):
            self.now += timedelta(minutes=16)
            return self.source(*args, **kwargs)
        self.assertEqual(self.run_capture(delayed)['new_attempts'], 0)

    def test_results_pair_by_full_identity_and_kickoff(self):
        self.run_capture()
        observations = ah.load_observations(self.root)
        self.now += timedelta(hours=3)
        result = {**self.fixture, 'status': 'completed', 'home_score': 4, 'away_score': 0}
        save_outcome(self.root, result, ah.stamp(self.now))
        paired, _ = ah.paired_rows(self.root, observations, self.now)
        self.assertEqual(len(paired), 1)
        self.assertEqual(ah.grouped_results(paired)['-2.75']['home']['full_win'], 1)
        result['kickoff_at'] = ah.stamp(ah.instant(result['kickoff_at']) + timedelta(hours=1))
        self.now += timedelta(minutes=1)
        save_outcome(self.root, result, ah.stamp(self.now))
        self.assertEqual(ah.paired_rows(self.root, observations, self.now)[0], [])

    def test_archive_is_period_specific_and_does_not_send_prior_to_start(self):
        self.assertEqual(ah.archive_month(self.root, datetime(2026, 9, 25, tzinfo=timezone.utc), None), {})
        self.run_capture()
        self.now += timedelta(hours=3)
        save_outcome(self.root, {**self.fixture, 'status': 'completed', 'home_score': 3, 'away_score': 0}, ah.stamp(self.now))
        self.assertEqual(ah.archive_month(self.root, datetime(2026, 11, 1, tzinfo=timezone.utc), None), {})
        archives = ah.archive_month(self.root, datetime(2026, 11, 1, 3, tzinfo=timezone.utc), None)
        self.assertIn('2026-10', archives)
        text = (self.root / 'data/f4/reports/monthly-2026-10.md').read_text(encoding='utf-8')
        self.assertIn('赛果配对：1场', text)
        self.assertIn('-2.75', text)
        self.assertIn('不是模型命中成绩', text)

    def test_batch_missing_data_is_not_no_selection(self):
        self.now += timedelta(minutes=20)
        ah.update_batches(self.root, [self.fixture], [], self.now, self.cfg)
        batch = ah.read(next((self.root / 'data/f4/ah/batches').glob('*.json')))
        self.assertEqual(batch['fixtures'][0]['status'], 'missed_window')
        self.assertFalse(batch['all_no_selection'])
        self.assertFalse(batch['direction_notification_enabled'])

    def test_cloud_critical_notice_does_not_read_current_month_archive(self):
        config = {**self.config, 'mode': 'shadow', 'candidate_weight': 0, 'experiment_id': 'synthetic',
                  'scope': 'league', 'league': 'england-premier', 'tracked_teams_path': 'teams.json',
                  'horizons_minutes': [180, 60], 'horizon_tolerance_minutes': 20, 'max_capture_age_minutes': 15}
        ah.write(self.root / 'data/f4/config.json', config)
        def collector(*args, **kwargs):
            return {'captured_at': ah.stamp(self.now), 'fixtures': [self.fixture], 'sources': [],
                    'coverage': {'complete': True}, 'problems': []}
        def broken_source(*args, **kwargs):
            return {'markets': {}, 'sources': [], 'problems': ['synthetic AH source failure']}
        with patch.object(cloud, 'outputs') as output:
            cloud.run(self.root, 'all', collector=collector, candidate=lambda *a: {},
                      f2_fetcher=lambda *a: {'sources': [], 'predictions': {}, 'problems': []},
                      ah_source=broken_source, clock=lambda: self.now)
        self.assertTrue(output.call_args.kwargs['notify'])
        notice = ah.read(self.root / '.github/run-logs/f4-notify-token.json')
        self.assertEqual(notice['type'], 'critical_incident')
        self.assertIn('synthetic AH source failure', (self.root / '.github/run-logs/f4-issue.md').read_text(encoding='utf-8'))

    def test_alternative_bookmaker_does_not_use_crown_early_snapshot(self):
        self.cfg['bookmaker_priority'] = ['crown', 'macau']
        ah.write(self.root / 'data/f4/config.json', self.config)
        self.now -= timedelta(minutes=45)
        self.quote['captured_at'] = ah.stamp(self.now)
        self.quote['published_at'] = ah.stamp(self.now)
        self.run_capture()
        self.now += timedelta(minutes=45)
        self.quote.update(bookmaker='macau', captured_at=ah.stamp(self.now), published_at=ah.stamp(self.now))
        self.run_capture()
        last = ah.load_observations(self.root)[-1]
        self.assertEqual(last['bookmaker'], 'macau')
        self.assertIsNone(last['early_quote'])


if __name__ == '__main__':
    unittest.main()
