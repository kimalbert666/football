"""Only synthetic quotes: preserve first-seen evidence and capture deadlines."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from f4_early_scout import background_window_safe, load_early_observations, scout_early_quotes
from f4_ah import read, write


class EarlyScoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
        write(self.root / 'data/f4/config.json', {'enrichment': {'first_observed_quotes': True},
            'asian_handicap': {'enabled': True, 'experiment_id': 'synthetic-only',
                              'max_capture_age_minutes': 5, 'bookmaker_priority': ['hkjc']}})
        self.fixture = {'provider': 'espn', 'league': 'england-premier', 'status': 'scheduled',
            'home_id': 'home', 'away_id': 'away', 'home': 'Home', 'away': 'Away',
            'match_id': 'synthetic-one', 'kickoff_at': '2026-10-12T19:00:00Z'}
        self.calls = 0

    def source(self, root, fixtures, now, **kwargs):
        self.calls += 1
        quote = {'bookmaker': 'hkjc', 'market': '90min_asian_handicap', 'home_handicap': -.25,
            'odds': [1.90, 1.90], 'captured_at': now.isoformat(), 'provider_event_id': 'synthetic-id',
            'url': 'https://example.invalid/synthetic', 'content_sha256': 'a' * 64}
        quote['market_1x2'] = {**quote, 'market': '90min_1x2', 'odds': [2, 3, 4]}
        return {'markets': {fixture['match_id']: quote for fixture in fixtures}}

    def scout(self, fixtures=None, source=None, **kwargs):
        return scout_early_quotes(self.root, fixtures or [self.fixture], self.now,
            source=source or self.source, clock=lambda: self.now, **kwargs)

    def test_first_observation_saved_separately_never_claims_initial_opening(self):
        result = self.scout()
        self.assertEqual(result['new_records'], 1)
        row = load_early_observations(self.root)[0]
        self.assertFalse(row['opening_certified'])
        self.assertEqual(row['horizon'], 'first-observed')
        self.assertFalse((self.root / 'data/f4/ah/observations').exists())
        original = next((self.root / 'data/f4/ah/first-observed').glob('*/*.json')).read_bytes()
        self.now += timedelta(days=1)
        self.assertEqual(self.scout()['new_records'], 0)
        self.assertEqual(self.calls, 1)
        self.assertEqual(next((self.root / 'data/f4/ah/first-observed').glob('*/*.json')).read_bytes(), original)

    def test_any_nearby_capture_window_defers_all_background_requests(self):
        for minutes in (10, 15, 20, 25, 40, 50, 60, 70, 90, 100, 160, 180, 200, 220, 230):
            fixture = {**self.fixture, 'kickoff_at': (self.now + timedelta(minutes=minutes)).isoformat()}
            with self.subTest(minutes=minutes):
                self.assertFalse(background_window_safe([fixture], self.now))
                self.assertEqual(self.scout([fixture])['status'], 'deferred_capture_window')
        self.assertEqual(self.calls, 0)
        self.assertTrue(background_window_safe([self.fixture], self.now))

    def test_missing_data_retries_next_day_not_repeated_each_run(self):
        missing = lambda *args, **kwargs: {'markets': {}}
        self.assertEqual(self.scout(source=missing)['status'], 'partial')
        self.assertEqual(self.scout()['status'], 'cached')
        self.assertEqual(self.calls, 0)
        self.now += timedelta(days=1)
        self.fixture['kickoff_at'] = (self.now + timedelta(days=1)).isoformat()
        self.assertEqual(self.scout()['new_records'], 1)

    def test_changed_kickoff_gets_new_identity_without_editing_old_record(self):
        self.scout()
        self.fixture['kickoff_at'] = '2026-10-13T19:00:00Z'
        result = self.scout(force=True)
        self.assertEqual(result['new_records'], 1)
        self.assertEqual(len(load_early_observations(self.root)), 2)

    def test_slow_source_cannot_label_late_price_as_early(self):
        def slow(*args, **kwargs):
            self.now += timedelta(hours=9)
            return self.source(*args, **kwargs)
        result = self.scout(source=slow)
        self.assertEqual(result['new_records'], 0)
        self.assertEqual(result['missing'][0]['reason'], 'early scouting deadline passed')

    def test_wrong_company_or_unpaired_1x2_is_rejected(self):
        for failure in ('wrong_book', 'missing_1x2'):
            def broken(*args, **kwargs):
                bundle = self.source(*args, **kwargs)
                quote = bundle['markets'][self.fixture['match_id']]
                if failure == 'wrong_book':
                    quote['bookmaker'] = 'unknown'
                else:
                    quote['market_1x2'] = None
                return bundle
            with self.subTest(failure=failure):
                self.assertEqual(self.scout(source=broken, force=True)['new_records'], 0)

    def test_corrupt_early_record_is_isolated_from_valid_cache(self):
        self.scout()
        path = self.root / 'data/f4/ah/first-observed/bad/bad.json'
        write(path, {'observed_at': self.now.isoformat(), 'quote': {'bookmaker': 'hkjc'}})
        issues = []
        rows = load_early_observations(self.root, issues=issues)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(issues), 1)

    def test_default_bulk_scout_preserves_primary_formal_request_quota(self):
        with patch('f4_hkjc.attach_hkjc', side_effect=self.source) as bulk, \
             patch('f4_ah_source.attach_asian_handicap') as primary:
            result = scout_early_quotes(self.root, [self.fixture], self.now, clock=lambda: self.now)
        self.assertEqual(result['new_records'], 1)
        bulk.assert_called_once()
        primary.assert_not_called()


if __name__ == '__main__':
    unittest.main()
