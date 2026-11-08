from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from f4_titan_features import TitanOpeningIndex, validate_feature_record


def decision():
    return {'match_id': 'espn:42', 'home_id': 'a', 'away_id': 'b',
            'kickoff_at': '2026-11-07T15:00:00Z', 'observed_at': '2026-11-07T14:45:00Z',
            'league': 'england-premier'}


def archive(captured='2026-11-06T04:00:00Z'):
    f = {'provider_event_id': '30042', 'canonical_match_id': 'espn:42',
         'home_id': 'a', 'away_id': 'b', 'kickoff_display': '2026-11-07 23:00',
         'independent_fixture_kickoff_at': '2026-11-07T15:00:00Z',
         'independent_fixture_last_seen_at': '2026-11-06T03:00:00Z',
         'mapping_source_evidence': {'status': 'ok', 'http_status': 200,
                                   'url': 'https://bf.titan007.com/football/Next_20261107.htm',
                                   'captured_at_utc': '2026-11-06T03:01:00Z'}}
    quote = {'home_handicap': -0.5, 'home_water_raw': '0.90', 'away_water_raw': '0.98'}
    opening = {**quote, 'displayed_full_datetime': '2026-10-26 06:45'}
    history = {'company_id': '3', 'provider_event_id': '30042',
               'captured_at_utc': '2026-11-06T03:59:00Z',
               'fixture_identity_verified': True, 'provider_opening_reconciled': True,
               'accepted_for_research_archive': True, 'fixture_kickoff_display': f['kickoff_display'],
               'projection_truncated': False, 'pagination_controls': [],
               'identity_check': {'identity_verified': True, 'event_id': '30042', 'company_id': '3', 'issues': []},
               'source_fixture_identity': f.copy(), 'source_reported_opening': opening,
               'earliest_visible_quote': {'state': 'unique_at_displayed_minute', 'quote': quote,
                                         'displayed_datetime': opening['displayed_full_datetime']},
               'source_url': 'https://vip.titan007.com/changeDetail/handicap.aspx?id=30042&companyID=3&l=0',
               'training_eligible': False, 'timezone_verified': False, 'odds_format_verified': False}
    return {'captured_at_utc': captured, 'fixture': f, 'histories': {'3': history},
            'status': 'displayed_history_reconciled'}


def load(payloads):
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        dest = root / 'data/f4/titan-history/observations/30042'
        dest.mkdir(parents=True)
        for number, value in enumerate(payloads):
            (dest / f'{number}.json').write_text(json.dumps(value), encoding='utf-8')
        return TitanOpeningIndex.from_root(root)


class TitanFeatureTests(unittest.TestCase):
    def test_native_opening_has_capture_lineage_and_no_invented_price_or_quote_time(self):
        index = load([archive()])
        feature, reason = index.select(decision())
        self.assertEqual(reason, 'available')
        self.assertEqual(feature['opening_home_handicap'], -0.5)
        self.assertEqual(feature['opening_home_water_raw'], 0.9)
        self.assertEqual(feature['water_scale'], 'provider_native_unconverted')
        self.assertIsNone(feature['quote_time_utc'])
        self.assertNotIn('home_decimal_odds', feature)
        self.assertEqual(len(feature['archive_sha256']), 64)

    def test_retrospective_and_after_decision_capture_are_unavailable(self):
        for captured in ('2026-11-07T14:46:00Z', '2026-11-08T00:00:00Z'):
            with self.subTest(captured=captured):
                self.assertIsNone(load([archive(captured)]).select(decision())[0])

    def test_future_archive_revision_does_not_contaminate_earlier_decision(self):
        future = archive('2026-11-08T00:00:00Z')
        future['histories']['3']['source_reported_opening']['home_water_raw'] = '0.95'
        future['histories']['3']['earliest_visible_quote']['quote']['home_water_raw'] = '0.95'
        feature, reason = load([future, archive()]).select(decision())
        self.assertEqual(reason, 'available')
        self.assertEqual(feature['opening_home_water_raw'], .9)

    def test_conflicting_available_openings_fail_and_repeated_agreement_uses_earliest(self):
        first = archive()
        repeat = archive('2026-11-07T04:00:00Z')
        feature, _ = load([repeat, first]).select(decision())
        self.assertEqual(feature['captured_at_utc'], first['captured_at_utc'])
        conflict = deepcopy(repeat)
        conflict['histories']['3']['source_reported_opening']['home_handicap'] = -.75
        conflict['histories']['3']['earliest_visible_quote']['quote']['home_handicap'] = -.75
        self.assertIsNone(load([first, conflict]).select(decision())[0])

    def test_identity_source_and_opening_mismatches_are_rejected(self):
        mutations = [
            lambda p: p['histories']['3'].update(source_url=p['histories']['3']['source_url']+'&id=42'),
            lambda p: p['histories']['3'].update(company_id='8'),
            lambda p: p['histories']['3'].update(provider_opening_reconciled=False),
            lambda p: p['histories']['3']['source_reported_opening'].update(home_handicap=-.75),
            lambda p: p['histories']['3'].update(projection_truncated=True),
            lambda p: p['histories']['3']['source_fixture_identity'].update(away_id='c'),
            lambda p: p['histories']['3']['source_reported_opening'].update(home_water_raw='NaN'),
            lambda p: p['fixture']['mapping_source_evidence'].update(status='restricted'),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bad = deepcopy(archive())
                mutate(bad)
                index = load([bad])
                self.assertIsNone(index.select(decision())[0])
                self.assertEqual(len(index.rejected), 1)

    def test_feature_revalidation_rejects_changed_fixture_or_cutoff(self):
        feature, _ = load([archive()]).select(decision())
        for change in ({'home_id': 'b', 'away_id': 'a'}, {'league': 'other'},
                       {'kickoff_at': '2026-11-08T15:00:00Z'},
                       {'observed_at': '2026-11-05T14:45:00Z'},
                       {'observed_at': '2026-11-07T15:00:00Z'}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    validate_feature_record(feature, {**decision(), **change})


if __name__ == '__main__':
    unittest.main()
