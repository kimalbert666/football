"""Offline end-to-end checks; synthetic fixtures never enter production data."""
import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
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

    def test_settlement_checkpoint_only_advances_for_verified_calendar(self):
        path = self.root / 'data/f4/status/last-settlement.json'
        self.run_cloud('capture')
        self.assertFalse(path.exists())
        self.run_cloud('daily')
        original = cloud.read_json(path, {})
        self.assertEqual(original['checked_at'], cloud.stamp(self.now))
        self.now += timedelta(days=1)
        def unavailable(*args, **kwargs):
            return {'captured_at': cloud.stamp(self.now), 'fixtures': [], 'sources': [],
                    'coverage': {'complete': False}, 'problems': ['synthetic unavailable calendar']}
        cloud.run(self.root, 'daily', collector=unavailable, candidate=self.candidate,
                  f2_fetcher=self.f2, clock=lambda: self.now)
        self.assertEqual(cloud.read_json(path, {}), original)

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

    def enable_ah(self):
        config = cloud.read_json(self.root / 'data/f4/config.json', {})
        config['asian_handicap'] = {
            'enabled': True, 'experiment_id': 'synthetic-ah-only', 'research_only': True,
            'automatic_promotion': False, 'automatic_wagering': False,
            'windows': {'180': 20, '60': 10, '15': 5}, 'max_capture_age_minutes': 5,
            'bookmaker_priority': ['hkjc'],
        }
        cloud.write_json(self.root / 'data/f4/config.json', config)

    def ah_source(self, root, fixtures, now, **kwargs):
        quotes = {}
        for fixture in fixtures:
            market = {'bookmaker': 'hkjc', 'market': '90min_1x2', 'odds': [2.0, 3.2, 4.0],
                      'captured_at': cloud.stamp(self.now), 'published_at': cloud.stamp(self.now),
                      'source': 'synthetic-hkjc', 'provider_event_id': fixture['match_id'],
                      'url': 'https://example.invalid/1x2', 'content_sha256': 'a' * 64}
            quotes[fixture['match_id']] = {
                **market, 'market': '90min_asian_handicap', 'home_handicap': -.25,
                'odds': [1.90, 1.95], 'market_1x2': market,
            }
        return {'markets': quotes, 'sources': [], 'problems': [], 'provider_faults': {}}

    def test_t15_ah_and_same_company_1x2_are_checkpointed_without_legacy_requests(self):
        import f4_ah as ah
        self.enable_ah()
        self.kickoff = self.now + timedelta(minutes=15)
        self.fixture['kickoff_at'] = cloud.stamp(self.kickoff)
        order = []
        def calendar(root, now, **kwargs):
            self.assertFalse(kwargs['include_market'])
            order.append('calendar')
            return self.collector(root, now, **kwargs)
        def source(*args, **kwargs):
            order.append('ah')
            return self.ah_source(*args, **kwargs)
        def checkpoint(root):
            rows = ah.load_observations(root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['quote_1x2']['bookmaker'], 'hkjc')
            self.assertIsNotNone(rows[0]['p_1x2'])
            order.append('checkpoint')
        def forbidden(*args, **kwargs):
            self.fail('T15-only capture must not request unrelated legacy sources')
        result = cloud.run(self.root, 'capture', collector=calendar, candidate=forbidden,
                           market_attacher=forbidden, f2_fetcher=forbidden, ah_source=source,
                           capture_checkpoint=checkpoint, clock=lambda: self.now)
        original = ah.load_observations(self.root)
        self.assertEqual(order, ['calendar', 'ah', 'checkpoint'])
        self.assertEqual(result['asian_handicap']['new_valid'], 1)
        self.now += timedelta(minutes=1)
        cloud.run(self.root, 'capture', collector=calendar, candidate=forbidden,
                  market_attacher=forbidden, f2_fetcher=forbidden, ah_source=source,
                  capture_checkpoint=checkpoint, clock=lambda: self.now)
        self.assertEqual(order, ['calendar', 'ah', 'checkpoint', 'calendar'])
        self.assertEqual(original, ah.load_observations(self.root))

    def test_slow_legacy_source_cannot_displace_due_t15_capture_or_start_late_f2(self):
        import f4_ah as ah
        self.enable_ah()
        at_capture = self.now
        t15 = {**self.fixture, 'match_id': 'espn:synthetic-t15',
               'kickoff_at': cloud.stamp(self.now + timedelta(minutes=15))}
        order = []
        def calendar(*args, **kwargs):
            self.assertFalse(kwargs['include_market'])
            return {**self.collector(*args, **kwargs), 'fixtures': [t15, copy.deepcopy(self.fixture)]}
        def checkpoint(root):
            self.assertEqual({r['horizon'] for r in ah.load_observations(root)}, {'T-15m', 'T-180m'})
            order.append('checkpoint')
        def slow_markets(root, bundle, **kwargs):
            self.assertEqual(order, ['checkpoint'])
            self.assertEqual(kwargs['match_ids'], {self.fixture['match_id']})
            self.assertEqual(len(ah.load_observations(root)), 2)
            self.now += timedelta(minutes=21)
            order.append('legacy')
            return {**bundle, 'captured_at': cloud.stamp(self.now)}
        result = cloud.run(self.root, 'capture', collector=calendar, candidate=self.candidate,
                           market_attacher=slow_markets, f2_fetcher=self.f2, ah_source=self.ah_source,
                           capture_checkpoint=checkpoint, clock=lambda: self.now)
        self.assertEqual(order, ['checkpoint', 'legacy'])
        self.assertEqual(result['asian_handicap']['new_valid'], 2)
        self.assertEqual(self.f2_calls, 0)
        self.assertEqual(cloud.load_predictions(self.root), [])
        self.assertTrue(result['skipped'])
        self.assertTrue(all(r['observed_at'] == cloud.stamp(at_capture) for r in ah.load_observations(self.root)))

    def test_slow_f2_can_cross_other_kickoff_only_after_ah_is_saved(self):
        import f4_ah as ah
        self.enable_ah()
        at_capture = self.now
        t15 = {**self.fixture, 'match_id': 'espn:synthetic-t15',
               'kickoff_at': cloud.stamp(self.now + timedelta(minutes=15))}
        order = []
        def calendar(*args, **kwargs):
            return {**self.collector(*args, **kwargs), 'fixtures': [t15, copy.deepcopy(self.fixture)]}
        def checkpoint(root):
            self.assertEqual(len(ah.load_observations(root)), 2)
            order.append('checkpoint')
        def markets(root, bundle, **kwargs):
            self.assertEqual(order, ['checkpoint'])
            order.append('legacy')
            return bundle
        def f2(*args):
            self.assertEqual(order, ['checkpoint', 'legacy'])
            order.append('f2')
            self.now += timedelta(minutes=16)
            return {'sources': [], 'predictions': {}, 'problems': []}
        result = cloud.run(self.root, 'capture', collector=calendar, candidate=self.candidate,
                           market_attacher=markets, f2_fetcher=f2, ah_source=self.ah_source,
                           capture_checkpoint=checkpoint, clock=lambda: self.now)
        self.assertEqual(order, ['checkpoint', 'legacy', 'f2'])
        self.assertEqual(result['new_valid_predictions'], 0)  # legacy quote exceeded its freshness limit
        self.assertEqual(result['asian_handicap']['new_valid'], 2)
        self.assertTrue(all(r['observed_at'] == cloud.stamp(at_capture) for r in ah.load_observations(self.root)))

    def test_priority_order_does_not_backdate_a_slow_ah_response(self):
        import f4_ah as ah
        self.enable_ah()
        self.kickoff = self.now + timedelta(minutes=15)
        self.fixture['kickoff_at'] = cloud.stamp(self.kickoff)
        def source(*args, **kwargs):
            self.now += timedelta(minutes=16)
            return self.ah_source(*args, **kwargs)
        checkpoint = Mock()
        legacy = Mock(side_effect=AssertionError('the expired window must not request old quotes'))
        result = cloud.run(self.root, 'capture', collector=self.collector, candidate=self.candidate,
                           market_attacher=legacy, f2_fetcher=self.f2, ah_source=source,
                           capture_checkpoint=checkpoint, clock=lambda: self.now)
        self.assertEqual(result['asian_handicap']['new_valid'], 0)
        self.assertEqual(ah.load_observations(self.root), [])
        checkpoint.assert_not_called()
        legacy.assert_not_called()

    def test_failed_priority_checkpoint_stops_legacy_work_without_claiming_persistence(self):
        import f4_ah as ah
        self.enable_ah()
        def broken_checkpoint(root):
            self.assertEqual(len(ah.load_observations(root)), 1)
            raise RuntimeError('synthetic checkpoint failed')
        legacy = Mock(side_effect=AssertionError('legacy must not start'))
        with self.assertRaisesRegex(RuntimeError, 'synthetic checkpoint failed'):
            cloud.run(self.root, 'capture', collector=self.collector, candidate=self.candidate,
                      market_attacher=legacy, f2_fetcher=legacy, ah_source=self.ah_source,
                      capture_checkpoint=broken_checkpoint, clock=lambda: self.now)
        legacy.assert_not_called()
        self.assertEqual(len(ah.load_observations(self.root)), 1)

    def test_legacy_market_failure_preserves_independent_ah_source_health_and_snapshot(self):
        import f4_ah as ah
        self.enable_ah()
        checkpoint = Mock()
        def unavailable(root, bundle, **kwargs):
            self.assertEqual(checkpoint.call_count, 1)
            return {**bundle, 'fixtures': [{**self.fixture, 'market': None}],
                    'problems': ['synthetic legacy Titan007 unavailable']}
        result = cloud.run(self.root, 'capture', collector=self.collector, candidate=self.candidate,
                           market_attacher=unavailable, f2_fetcher=self.f2, ah_source=self.ah_source,
                           capture_checkpoint=checkpoint, clock=lambda: self.now)
        self.assertEqual(result['asian_handicap']['new_valid'], 1)
        self.assertEqual(result['new_valid_predictions'], 0)
        self.assertEqual(ah.read(self.root / 'data/f4/ah/source-health.json')['problems'], [])
        self.assertEqual(ah.load_observations(self.root)[0]['quote_1x2']['bookmaker'], 'hkjc')
        self.assertIn('synthetic legacy Titan007 unavailable', result['source_health']['problems'])

    def test_missing_t15_defers_slow_legacy_then_recovers_on_true_next_minute(self):
        import f4_ah as ah
        self.enable_ah()
        first_at = self.now
        t15 = {**self.fixture, 'match_id': 'espn:synthetic-t15',
               'kickoff_at': cloud.stamp(self.now + timedelta(minutes=15)), 'market': None}
        source_calls, legacy_calls = [], []
        def calendar(*args, **kwargs):
            return {**self.collector(*args, **kwargs), 'fixtures': [t15, copy.deepcopy(self.fixture)]}
        def source(root, fixtures, now, **kwargs):
            source_calls.append(now)
            result = self.ah_source(root, fixtures, now, **kwargs)
            if len(source_calls) == 1:
                result['markets'].pop(t15['match_id'])
                result['problems'] = ['synthetic T15 quote temporarily missing']
            return result
        def slow_markets(root, bundle, **kwargs):
            final_rows = [r for r in ah.load_observations(root) if r['match_id'] == t15['match_id']]
            self.assertTrue(any(r.get('quote') and r.get('p_1x2') for r in final_rows))
            legacy_calls.append(self.now)
            self.now += timedelta(minutes=21)
            return {**bundle, 'captured_at': cloud.stamp(self.now)}
        checkpoint = Mock()
        first = cloud.run(self.root, 'capture', collector=calendar, candidate=self.candidate,
                          market_attacher=slow_markets, f2_fetcher=self.f2, ah_source=source,
                          capture_checkpoint=checkpoint, clock=lambda: self.now)
        self.assertEqual(self.now, first_at)
        self.assertEqual(legacy_calls, [])
        self.assertEqual(self.f2_calls, 0)
        self.assertEqual(first['legacy_deferred']['pending_t15_matches'], [t15['match_id']])
        self.assertEqual(first['legacy_deferred']['deferred_legacy_matches'], [self.fixture['match_id']])
        self.assertTrue(first['skipped'])
        original_missing = next(r for r in ah.load_observations(self.root) if r['match_id'] == t15['match_id'])
        self.assertIsNone(original_missing['quote'])
        self.now += timedelta(minutes=1)
        second = cloud.run(self.root, 'capture', collector=calendar, candidate=self.candidate,
                           market_attacher=slow_markets, f2_fetcher=self.f2, ah_source=source,
                           capture_checkpoint=checkpoint, clock=lambda: self.now)
        self.assertEqual(source_calls, [first_at, first_at + timedelta(minutes=1)])
        self.assertEqual(legacy_calls, [first_at + timedelta(minutes=1)])
        self.assertEqual(checkpoint.call_count, 2)
        self.assertIsNone(second['legacy_deferred'])
        self.assertEqual(self.f2_calls, 0)  # its own window expired during the later legacy request
        rows = ah.load_observations(self.root)
        self.assertIn(original_missing, rows)
        final = next(r for r in rows if r['match_id'] == t15['match_id'] and r.get('quote'))
        self.assertEqual(final['observed_at'], cloud.stamp(first_at + timedelta(minutes=1)))
        self.assertIsNotNone(final['p_1x2'])

    def test_t15_ah_only_record_cannot_unlock_legacy_without_same_company_1x2(self):
        import f4_ah as ah
        self.enable_ah()
        t15 = {**self.fixture, 'match_id': 'espn:synthetic-t15',
               'kickoff_at': cloud.stamp(self.now + timedelta(minutes=15)), 'market': None}
        def calendar(*args, **kwargs):
            return {**self.collector(*args, **kwargs), 'fixtures': [t15, copy.deepcopy(self.fixture)]}
        def source(*args, **kwargs):
            result = self.ah_source(*args, **kwargs)
            result['markets'][t15['match_id']]['market_1x2'] = None
            return result
        legacy = Mock(side_effect=AssertionError('incomplete T15 must protect the retry window'))
        result = cloud.run(self.root, 'capture', collector=calendar, candidate=self.candidate,
                           market_attacher=legacy, f2_fetcher=legacy, ah_source=source,
                           capture_checkpoint=Mock(), clock=lambda: self.now)
        legacy.assert_not_called()
        final = next(r for r in ah.load_observations(self.root) if r['match_id'] == t15['match_id'])
        self.assertIsNotNone(final['quote'])
        self.assertIsNone(final['p_1x2'])
        self.assertEqual(result['legacy_deferred']['reason'], 'incomplete_t15_joint_market')


if __name__ == '__main__':
    unittest.main()
