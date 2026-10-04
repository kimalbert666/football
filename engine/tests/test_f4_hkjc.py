"""Synthetic read-only adapter tests; none of these quotes enter research data."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from f4_hkjc import (URL, VARIABLES, attach_hkjc, fetch, line_value,
                     matches_fixture, parse_market, prices, validate_response)


class HkjcTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 10, 11, 15, tzinfo=timezone.utc)
        self.captured = '2026-10-10T11:15:00Z'
        self.aliases = {'arsenal': {'arsenal'}, 'leeds': {'leeds'}}
        self.fixture = {'match_id': 'espn:synthetic', 'provider': 'espn', 'league': 'england-premier',
                        'status': 'scheduled', 'home_id': 'arsenal', 'away_id': 'leeds',
                        'kickoff_at': '2026-10-10T11:30:00Z'}
        self.row = {'id': 'hkjc-synthetic', 'status': 'PREEVENT',
                    'kickOffTime': self.fixture['kickoff_at'],
                    'tournament': {'name_en': 'English Premier'},
                    'homeTeam': {'id': 'home-synthetic', 'name_en': 'Arsenal'},
                    'awayTeam': {'id': 'away-synthetic', 'name_en': 'Leeds'},
                    'poolInfo': {'sellingPools': ['HAD', 'HDC']},
                    'foPools': [self.pool('HAD', [self.line('0', ('H', 'D', 'A'), ('1.5', '4.0', '6.0'))]),
                                self.pool('HDC', [self.line('-0.5/-1', ('H', 'A'), ('1.9', '1.9'))])]}
        self.received = {'payload': {'data': {'matches': [self.row]}},
                         'headers': {'Date': 'Sat, 10 Oct 2026 11:15:00 GMT',
                                     'Cache-Control': 'public, max-age=30'},
                         'captured_at': self.captured}

    @staticmethod
    def line(condition, sides, odds, **kwargs):
        return {'condition': condition, 'status': 'AVAILABLE', 'main': False,
                'combinations': [{'str': side, 'status': 'AVAILABLE', 'currentOdds': odd,
                                  'selections': [{'str': side}]} for side, odd in zip(sides, odds)], **kwargs}

    @staticmethod
    def pool(kind, lines):
        return {'oddsType': kind, 'status': 'SELLINGSTARTED', 'inplay': True,
                'updateAt': '2026-10-09T12:00:00Z', 'lines': lines}

    def parse(self, row=None, captured=None):
        return parse_market(row or self.row, captured or self.captured, self.captured, 'a'*64)

    def test_signed_quarter_lines_are_home_perspective(self):
        for text, expected in {'-0.5/-1': -.75, '-1/-1.5': -1.25, '0/+0.5': .25,
                               '-0.5/0': -.25, '-3.5': -3.5, '0': 0, '+3/+3.5': 3.25}.items():
            with self.subTest(text=text):
                self.assertEqual(line_value(text), expected)
        for value in (True, None, '-0.25', '1/2', '1/1', '-1/+1', 'NaN', '0.5 text', '1' * 400):
            with self.subTest(value=value), self.assertRaises(ValueError):
                line_value(value)

    def test_price_direction_is_identity_order_not_array_order(self):
        line = self.line('-1', ('A', 'H'), ('2.2', '1.8'))
        self.assertEqual(prices(line, ('H', 'A')), [1.8, 2.2])
        for changes in ({'currentOdds': True}, {'currentOdds': 'nan'}, {'currentOdds': '1'},
                        {'status': 'SUSPENDED'}, {'selections': [{'str': 'H'}]}):
            test_line = copy.deepcopy(line)
            test_line['combinations'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                prices(test_line, ('H', 'A'))
        for combinations in ([line['combinations'][0]], line['combinations'] * 2):
            with self.assertRaises(ValueError):
                prices({**line, 'combinations': combinations}, ('H', 'A'))

    def test_fresh_http_does_not_relabel_old_publication_as_opening(self):
        result = self.parse()
        self.assertEqual(result['home_handicap'], -.75)
        self.assertEqual(result['published_at'], '2026-10-09T12:00:00Z')
        self.assertEqual(result['snapshot_at'], self.captured)
        self.assertEqual(result['opening_lines'], [])
        self.assertEqual(result['bookmaker'], result['market_1x2']['bookmaker'])
        self.assertEqual(result['source'], 'hkjc-direct')
        self.assertIn('no execution guarantee', result['provenance'])

    def test_main_line_or_balance_choice_preserves_alternatives(self):
        self.row['foPools'][1]['lines'].append(self.line('-1', ('H', 'A'), ('2.1', '1.7'), main=True))
        result = self.parse()
        self.assertEqual(result['home_handicap'], -1)
        self.assertEqual(result['line_selection'], 'provider_main')
        self.assertEqual(len(result['lines']), 2)
        self.row['foPools'][1]['lines'][1]['main'] = False
        self.assertEqual(self.parse()['home_handicap'], -.75)
        self.assertEqual(self.parse()['line_selection'], 'closest_price_balance_not_provider_main')
        for line in self.row['foPools'][1]['lines']:
            line['main'] = True
        with self.assertRaises(ValueError):
            self.parse()

    def test_rejects_live_suspended_missing_ambiguous_and_future_markets(self):
        variants = []
        for status in ('FIRSTHALF', 'COMPLETED'):
            variants.append({**self.row, 'status': status})
        variants.append({**self.row, 'poolInfo': {'sellingPools': ['HDC']}})
        variants.append({**self.row, 'foPools': self.row['foPools'] + [self.row['foPools'][0]]})
        for field, value in [('status', 'SUSPENDED'), ('updateAt', '2026-10-10T11:15:01Z')]:
            row = copy.deepcopy(self.row)
            row['foPools'][1][field] = value
            variants.append(row)
        row = copy.deepcopy(self.row)
        row['foPools'][1]['lines'].append(copy.deepcopy(row['foPools'][1]['lines'][0]))
        variants.append(row)
        for row in variants:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.parse(row)
        with self.assertRaises(ValueError):
            self.parse(captured=self.fixture['kickoff_at'])

    def test_identity_requires_epl_canonical_teams_scheduled_time_and_ids(self):
        self.assertTrue(matches_fixture(self.row, self.fixture, self.aliases))
        for row in (None, {}, {**self.row, 'kickOffTime': 'malformed'},
                    {**self.row, 'kickOffTime': '2026-10-10T11:32:00Z'},
                    {**self.row, 'tournament': {'name_en': 'FA Cup'}},
                    {**self.row, 'homeTeam': {'id': 'home-synthetic', 'name_en': 'Unknown'}},
                    {**self.row, 'homeTeam': {**self.row['homeTeam'], 'id': 'away-synthetic'}},
                    {**self.row, 'awayTeam': {**self.row['awayTeam'], 'id': None}}):
            with self.subTest(row=row):
                self.assertFalse(matches_fixture(row, self.fixture, self.aliases))
        for changes in ({'status': 'postponed'}, {'league': 'FA Cup'}, {'provider': 'other'},
                        {'home_id': None, 'away_id': None}, {'away_id': 'arsenal'}):
            with self.subTest(changes=changes):
                self.assertFalse(matches_fixture(self.row, {**self.fixture, **changes}, self.aliases))

    def test_http_cache_and_graphql_errors_are_fail_closed(self):
        self.assertEqual(validate_response(self.received)[1], self.captured)
        for changes in ({'Date': 'Sat, 10 Oct 2026 11:12:59 GMT'},
                        {'Date': 'Sat, 10 Oct 2026 11:15:06 GMT'}, {'Date': 'Sat, 10 Oct 2026 11:15:00'},
                        {'Age': '61'}, {'Age': '-1'}, {'Age': 'nan'},
                        {'Cache-Control': 'public, max-age=300'}, {'Cache-Control': 'no-store'}):
            received = {**self.received, 'headers': {**self.received['headers'], **changes}}
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                validate_response(received)
        for payload in ({'errors': [{'message': 'partial'}], **self.received['payload']},
                        {'data': {'matches': None}}, None):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_response({**self.received, 'payload': payload})

    def root(self, directory):
        root = Path(directory)
        aliases = root / 'data/01-teams/_aliases.json'
        aliases.parent.mkdir(parents=True)
        aliases.write_text(json.dumps({'england': {'arsenal': {'name': 'Arsenal'},
                                                 'leeds': {'name': 'Leeds'}}}), encoding='utf-8')
        return root

    def test_attachment_is_read_only_and_tolerates_unrelated_malformed_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory)
            self.received['payload']['data']['matches'].extend([None, {'tournament': None},
                                                               {**self.row, 'kickOffTime': 'malformed'}])
            before = sorted(str(path.relative_to(root)) for path in root.rglob('*'))
            result = attach_hkjc(root, [self.fixture], self.now, transport=lambda: self.received)
            self.assertEqual(set(result['markets']), {'espn:synthetic'})
            self.assertEqual(result['problems'], [])
            self.assertEqual(before, sorted(str(path.relative_to(root)) for path in root.rglob('*')))

    def test_duplicate_identity_missing_market_or_rejected_query_never_creates_quote(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory)
            for rows in ([self.row, self.row], [], [{**self.row, 'foPools': []}]):
                received = {**self.received, 'payload': {'data': {'matches': rows}}}
                result = attach_hkjc(root, [self.fixture], self.now, transport=lambda: received)
                self.assertEqual(result['markets'], {})
                self.assertTrue(result['problems'])
            self.received['payload']['errors'] = [{'message': 'WHITELIST_ERROR'}]
            result = attach_hkjc(root, [self.fixture], self.now, transport=lambda: self.received)
            self.assertEqual(result['sources'][0]['status'], 'error')
            self.assertEqual(result['markets'], {})

    def test_no_eligible_fixture_does_not_call_network_unless_probe(self):
        transport = Mock(return_value=self.received)
        invalid = [{**self.fixture, 'status': 'live'}, {**self.fixture, 'home_id': None},
                   {**self.fixture, 'kickoff_at': '2026-10-10T11:15:00Z'}]
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory)
            self.assertEqual(attach_hkjc(root, invalid, self.now, transport=transport)['markets'], {})
            transport.assert_not_called()
            result = attach_hkjc(root, [], self.now, transport=transport, probe=True)
            transport.assert_called_once()
            self.assertEqual(result['markets'], {})
            self.assertEqual(result['sources'][0]['status'], 'ok')
        with self.assertRaises(ValueError):
            attach_hkjc('.', [], self.now.replace(tzinfo=None), transport=transport)

    def test_transport_calls_only_public_query_and_disallows_redirects(self):
        response = Mock(status_code=200, headers=self.received['headers'])
        response.json.return_value = self.received['payload']
        with patch('requests.post', return_value=response) as post:
            received = fetch()
        self.assertEqual(received['payload'], self.received['payload'])
        args, kwargs = post.call_args
        self.assertEqual(args, (URL,))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['json']['variables'], VARIABLES)
        self.assertTrue(kwargs['json']['query'].lstrip().startswith('query matchList('))
        self.assertNotIn('Authorization', kwargs.get('headers', {}))
        response.status_code = 302
        with patch('requests.post', return_value=response), self.assertRaises(ValueError):
            fetch()


if __name__ == '__main__':
    unittest.main()
