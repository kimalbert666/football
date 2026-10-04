"""The independent fallback must preserve provider faults and experiment scope."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from f4_ah_source import attach_asian_handicap


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 10, 11, 15, tzinfo=timezone.utc)
        self.fixtures = [{'match_id': 'espn:synthetic-a'}, {'match_id': 'espn:synthetic-b'}]

    def root(self, directory, books=('crown', 'hkjc')):
        root = Path(directory)
        path = root / 'data/f4/config.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'asian_handicap': {'bookmaker_priority': list(books)}}), encoding='utf-8')
        return root

    @staticmethod
    def result(markets=None, problems=None, health=None):
        return {'markets': markets or {}, 'problems': problems or [],
                'sources': [{'status': 'ok'}], 'provider_health': health or {}}

    def test_fallback_only_fills_missing_matches_and_keeps_primary_fault(self):
        primary = Mock(return_value=self.result({'espn:synthetic-a': {'bookmaker': 'crown'}},
                                                ['primary quote missing'], {'feed_live': False}))
        backup = Mock(return_value=self.result({'espn:synthetic-b': {'bookmaker': 'hkjc'}}))
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory)
            out = attach_asian_handicap(root, self.fixtures, self.now, primary=primary, fallback=backup)
            backup.assert_called_once_with(root, [self.fixtures[1]], self.now, probe=False)
            self.assertEqual(out['quote_availability'], 'complete')
            self.assertEqual(out['markets']['espn:synthetic-a']['bookmaker'], 'crown')
            self.assertEqual(out['fallback_matches'], ['espn:synthetic-b'])
            self.assertEqual(out['provider_faults']['infersports'], ['primary quote missing'])
            self.assertEqual(out['problems'], ['primary quote missing'])
            self.assertEqual(out['primary_provider_health'], {'feed_live': False})

    def test_no_fallback_when_primary_quotes_complete(self):
        primary = Mock(return_value=self.result({f['match_id']: {'bookmaker': 'crown'} for f in self.fixtures}))
        backup = Mock()
        with tempfile.TemporaryDirectory() as directory:
            out = attach_asian_handicap(self.root(directory), self.fixtures, self.now, primary=primary, fallback=backup)
            backup.assert_not_called()
            self.assertEqual(out['unresolved_matches'], [])

    def test_excluded_company_never_polled_even_for_probe(self):
        primary, backup = Mock(return_value=self.result()), Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory, ('crown', 'macau'))
            out = attach_asian_handicap(root, self.fixtures, self.now, probe=True, primary=primary, fallback=backup)
            backup.assert_not_called()
            self.assertEqual(out['quote_availability'], 'incomplete')
            self.assertEqual(out['sources'][-1]['status'], 'disabled')

    def test_probe_without_fixtures_does_not_claim_quote_availability(self):
        primary = Mock(return_value=self.result(problems=['feed stale']))
        backup = Mock(return_value=self.result())
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory)
            out = attach_asian_handicap(root, [], self.now, probe=True, primary=primary, fallback=backup)
            backup.assert_called_once_with(root, [], self.now, probe=True)
            self.assertEqual(out['quote_availability'], 'not_tested_no_due_matches')
            self.assertEqual(out['markets'], {})
            self.assertEqual(out['problems'], ['feed stale'])

    def test_both_provider_faults_and_unresolved_games_remain_visible(self):
        primary = Mock(return_value=self.result(problems=['feed stale']))
        backup = Mock(return_value=self.result(problems=['independent source down']))
        with tempfile.TemporaryDirectory() as directory:
            out = attach_asian_handicap(self.root(directory), self.fixtures, self.now, primary=primary, fallback=backup)
            self.assertEqual(out['unresolved_matches'], [f['match_id'] for f in self.fixtures])
            self.assertEqual(out['quote_availability'], 'incomplete')
            self.assertEqual(out['problems'], ['feed stale', 'independent source down'])


if __name__ == '__main__':
    unittest.main()
