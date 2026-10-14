"""Optional background sources cannot replace or delay fixed-window quotes."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import f4_ah as ah
import f4_cloud as cloud


class EnrichmentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 14, 10, 45, tzinfo=timezone.utc)
        self.fixture = {'match_id': 'espn:synthetic-only', 'provider': 'espn', 'source_event_id': '1',
                        'league': 'england-premier', 'home_id': 'home', 'away_id': 'away',
                        'home': 'Synthetic home', 'away': 'Synthetic away',
                        'kickoff_at': '2026-10-14T11:00:00Z', 'status': 'scheduled', 'market': None}
        self.config = {'mode': 'shadow', 'candidate_weight': 0, 'experiment_id': 'synthetic-only',
            'scope': 'league', 'league': 'england-premier', 'tracked_teams_path': 'teams.json',
            'horizons_minutes': [180, 60], 'horizon_tolerance_minutes': 20,
            'max_capture_age_minutes': 5, 'enrichment': {'enabled': True,
                'independent_strength': True, 'historical_xg_and_availability': True,
                'first_observed_quotes': True, 'capture_window_buffer_minutes': 30},
            'asian_handicap': {'enabled': True, 'experiment_id': 'synthetic-ah', 'research_only': True,
                'automatic_promotion': False, 'automatic_wagering': False, 'windows': {'180': 20, '60': 10, '15': 5},
                'max_capture_age_minutes': 5, 'bookmaker_priority': ['hkjc']},
            'notifications': {'policy': 'monthly_and_critical_only', 'started_at': '2026-09-23T00:00:00Z'}}
        ah.write(self.root / 'data/f4/config.json', self.config)

    def collector(self, *args, **kwargs):
        return {'captured_at': ah.stamp(self.now), 'fixtures': [self.fixture],
                'sources': [], 'problems': [], 'coverage': {'complete': True}}

    def source(self, *args, **kwargs):
        quote = {'bookmaker': 'hkjc', 'market': '90min_asian_handicap', 'home_handicap': -.25,
                 'odds': [1.90, 1.90], 'captured_at': ah.stamp(self.now), 'url': 'https://example.invalid/synthetic',
                 'provider_event_id': 'synthetic', 'content_sha256': 'a' * 64}
        quote['market_1x2'] = {**quote, 'market': '90min_1x2', 'odds': [2, 3, 4]}
        return {'markets': {self.fixture['match_id']: quote}, 'sources': [], 'problems': []}

    def run_cloud(self, command):
        return cloud.run(self.root, command, collector=self.collector, ah_source=self.source,
            candidate=lambda *args: {}, f2_fetcher=lambda *args: {'sources': [], 'predictions': {}, 'problems': []},
            market_attacher=lambda root, bundle, **kwargs: bundle, clock=lambda: self.now)

    def test_capture_uses_only_existing_context_cache_and_never_refreshes(self):
        ratings = {'home': 1510, 'away': 1490, 'prior_games': [6, 6],
                   'observed_at': ah.stamp(self.now - timedelta(hours=2)), 'source': 'synthetic-rating'}
        context = {'observed_at': ah.stamp(self.now - timedelta(hours=2)), 'synthetic': True}
        with patch('f4_strength.ratings_for_fixture', return_value=ratings) as read_rating, \
             patch('f4_context.context_for_fixture', return_value=context) as read_context, \
             patch('f4_strength.refresh_strength') as refresh_rating, \
             patch('f4_context.refresh_context') as refresh_context:
            self.run_cloud('capture')
        row = ah.load_observations(self.root)[0]
        self.assertEqual(row['ratings'], ratings)
        self.assertEqual(row['context'], context)
        self.assertEqual(row['context_feature_policy'], 'stored_for_future_protocol_not_used_by_v2')
        read_rating.assert_called_once()
        read_context.assert_called_once()
        refresh_rating.assert_not_called()
        refresh_context.assert_not_called()

    def test_background_window_guard_prevents_daily_optional_fetches(self):
        with patch('f4_strength.refresh_strength') as strength, \
             patch('f4_context.refresh_context') as context, \
             patch('f4_early_scout.scout_early_quotes') as scout:
            self.run_cloud('daily')
        strength.assert_not_called()
        context.assert_not_called()
        scout.assert_not_called()
        saved = ah.read(self.root / 'data/f4/status/latest.json')
        self.assertEqual(saved['enrichment']['refresh_status'], 'deferred_capture_window')

    def test_background_errors_do_not_discard_completed_quote_records(self):
        self.run_cloud('capture')
        observation_path = next((self.root / 'data/f4/ah/observations').glob('*/*.json'))
        original_quote = observation_path.read_bytes()
        self.fixture['kickoff_at'] = '2026-10-14T23:00:00Z'
        with patch('f4_strength.refresh_strength', side_effect=ValueError('synthetic failure')), \
             patch('f4_context.refresh_context', return_value={'status': 'ready'}), \
             patch('f4_early_scout.scout_early_quotes', return_value={'status': 'complete', 'new_records': 0}):
            self.run_cloud('all')
        status = ah.read(self.root / 'data/f4/status/enrichment-latest.json')
        self.assertEqual(status['sources']['strength']['status'], 'unavailable')
        self.assertEqual(status['sources']['context']['status'], 'ready')
        self.assertTrue((self.root / 'data/f4/ah/latest.json').exists())
        self.assertEqual(observation_path.read_bytes(), original_quote)

    def test_disabled_optional_features_are_not_used_even_if_cache_exists(self):
        self.config['enrichment']['enabled'] = False
        ah.write(self.root / 'data/f4/config.json', self.config)
        with patch('f4_strength.ratings_for_fixture') as rating, \
             patch('f4_context.context_for_fixture') as context:
            self.run_cloud('capture')
        rating.assert_not_called()
        context.assert_not_called()
        self.assertIsNone(ah.load_observations(self.root)[0]['context'])

    def test_full_acceptance_forces_source_refresh_beyond_published_cache(self):
        self.fixture['kickoff_at'] = '2026-10-14T23:00:00Z'
        with patch('f4_strength.refresh_strength', return_value={'status': 'ready'}) as strength, \
             patch('f4_context.refresh_context', return_value={'status': 'ready'}) as context, \
             patch('f4_early_scout.scout_early_quotes', return_value={'status': 'partial'}) as scout:
            self.run_cloud('all')
        self.assertTrue(strength.call_args.kwargs['force'])
        self.assertTrue(context.call_args.kwargs['force'])
        self.assertTrue(scout.call_args.kwargs['force'])


if __name__ == '__main__':
    unittest.main()
