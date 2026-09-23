import copy
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from f4_ah_source import parse_quotes, attach_crown_ah, LEAGUE_ID, _stamp


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, 10, 45, tzinfo=timezone.utc)
        self.aliases = {'arsenal': {'arsenal'}, 'leeds': {'leeds'}}
        self.event = {'id': 'evt_test', 'sport': 'football', 'status': 'scheduled',
                      'league': {'id': LEAGUE_ID, 'name': 'ENPL'},
                      'home_team': {'id': 'tm_a', 'name': 'Arsenal'}, 'away_team': {'id': 'tm_b', 'name': 'Leeds'},
                      'scheduled_at': '2026-10-01T11:00:00Z'}
        self.fixture = {'match_id': 'espn:synthetic', 'home_id': 'arsenal', 'away_id': 'leeds',
                        'kickoff_at': self.event['scheduled_at'], 'provider': 'espn', 'status': 'scheduled',
                        'league': 'england-premier'}
        self.quote = {'bookmaker': 'crown', 'period': 'full_time', 'market_type': 'asian_handicap',
                      'line': -2.75, 'handicap_components': [-2.5, -3.0], 'prices': {'home': 1.9, 'away': 2.0},
                      'format': 'decimal', 'status': 'open', 'as_of': '2026-10-01T09:00:00Z'}
        self.payload = {'event_id': 'evt_test', 'as_of': _stamp(self.now - timedelta(seconds=90)),
                        'stale': False, 'odds': [self.quote]}

    def parse(self, payload=None, event=None):
        return parse_quotes(payload or self.payload, event or self.event, self.fixture, self.aliases,
                            _stamp(self.now), 'https://example.invalid/synthetic', 'a'*64)

    def test_preserves_all_lines_and_does_not_claim_main_line(self):
        self.payload['odds'].append({**self.quote, 'line': 3.5, 'handicap_components': None,
                                     'prices': {'home': 1.95, 'away': 1.95}})
        result = self.parse()
        self.assertEqual(result['home_handicap'], 3.5)
        self.assertEqual(len(result['lines']), 2)
        self.assertEqual(result['line_selection'], 'closest_price_balance_not_provider_main')

    def test_rejects_stale_feed_future_or_live_identity(self):
        for changes in ({'stale': True}, {'as_of': _stamp(self.now - timedelta(minutes=10))},
                        {'as_of': _stamp(self.now + timedelta(seconds=1))}, {'event_id': 'other'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.parse({**self.payload, **changes})
        for changes in ({'status': 'live'}, {'scheduled_at': '2026-10-01T12:00:00Z'},
                        {'league': {'id': LEAGUE_ID, 'name': 'FA Cup'}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.parse(event={**self.event, **changes})

    def test_does_not_use_other_bookmakers_half_time_or_suspended_quotes(self):
        for changes in ({'bookmaker': 'other'}, {'period': 'half_time'}, {'status': 'suspended'},
                        {'format': 'hk'}, {'line': .3}, {'prices': {'home': 0, 'away': 0}},
                        {'handicap_components': [-2, -3]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.parse({**self.payload, 'odds': [{**self.quote, **changes}]})

    def test_valid_opening_is_labelled_provider_reported(self):
        opening = {'event_id': 'evt_test', 'opening': [{**self.quote, 'opened_at': '2026-09-30T00:00:00Z'}]}
        result = parse_quotes(self.payload, self.event, self.fixture, self.aliases,
                              _stamp(self.now), 'https://example.invalid/synthetic', 'a'*64, opening)
        self.assertEqual(result['opening_lines'][0]['home_handicap'], -2.75)
        self.assertIn('not independently certified', result['opening_lines'][0]['provenance'])

    def test_unchanged_prices_keep_original_timestamp_with_fresh_health_evidence(self):
        old = {**self.payload, 'as_of': '2026-10-01T09:00:00Z'}
        result = parse_quotes(old, self.event, self.fixture, self.aliases, _stamp(self.now),
                              'https://example.invalid/synthetic', 'a'*64,
                              health={'feed_live': True, 'as_of_stale': False, 'as_of': _stamp(self.now)})
        self.assertEqual(result['published_at'], '2026-10-01T09:00:00Z')
        self.assertEqual(result['snapshot_at'], '2026-10-01T09:00:00Z')
        self.assertIn('global feed health', result['freshness_evidence'])

    def test_explicit_alternative_bookmaker_is_preserved(self):
        payload = {**self.payload, 'odds': [{**self.quote, 'bookmaker': 'macau'}]}
        result = parse_quotes(payload, self.event, self.fixture, self.aliases, _stamp(self.now),
                              'https://example.invalid/synthetic', 'a'*64, bookmaker='macau')
        self.assertEqual(result['bookmaker'], 'macau')

    def test_no_due_match_does_not_spend_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            def fail(url):
                raise AssertionError('unexpected network call')
            result = attach_crown_ah(Path(directory), [], self.now, fetch=fail)
            self.assertEqual(result['problems'], [])
            self.assertEqual(result['sources'][0]['status'], 'not_requested')


if __name__ == '__main__':
    unittest.main()
