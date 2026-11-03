import json
from pathlib import Path
import unittest
from titan_batch_reader import same_destination, validate_targets, prepare_archive, PROJECTION, archive_acceptance


class BatchContractTests(unittest.TestCase):
    def test_boundary_exclusion_retains_valid_history_but_other_errors_fail(self):
        a = {'fixture_identity_verified': True, 'provider_opening_reconciled': True,
             'prematch_quote_count': 2, 'projection_truncated': False, 'pagination_controls': [],
             'parse_ok': False, 'fixture_kickoff_display': '2026-10-17 22:00',
             'rows': [{'issues': ['prematch_not_before_kickoff'], 'prematch_quote': False,
                       'displayed_datetime': '2026-10-17 22:00'}]}
        self.assertTrue(archive_acceptance(a)['accepted_for_research_archive'])
        self.assertEqual(archive_acceptance(a)['excluded_boundary_row_count'], 1)
        for change in ({'provider_opening_reconciled': False}, {'projection_truncated': True},
                       {'pagination_controls': ['下一页']}, {'prematch_quote_count': 0},
                       {'rows': [{'issues': ['unparseable_quote']}]},
                       {'rows': [{'issues': ['prematch_not_before_kickoff', 'prematch_status_has_clock_or_score']}]}):
            self.assertFalse(archive_acceptance({**a, **change})['accepted_for_research_archive'])

    def test_redirect_rejects_wrong_event_company_market(self):
        url = 'https://vip.titan007.com/changeDetail/handicap.aspx?id=42&companyID=3&l=0'
        self.assertTrue(same_destination(url, url))
        for bad in (url.replace('id=42', 'id=43'), url.replace('companyID=3', 'companyID=8'),
                    url.replace('l=0', 'l=1'), url + '&id=42', url.replace('https:', 'http:'),
                    url.replace('vip.', 'evil.'), url + '#bad'):
            with self.subTest(bad=bad):
                self.assertFalse(same_destination(url, bad))

    def test_target_validation_is_bounded_and_unique(self):
        f = {'provider_event_id': '42', 'home': 'A', 'away': 'B', 'kickoff_display': '2026-10-17 22:00'}
        validate_targets([f])
        for bad in ([], [f] * 11, [f, f], [{**f, 'provider_event_id': '../42'}],
                    [{**f, 'company_ids': ['3', '3']}], [{**f, 'away': 'A'}]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_targets(bad)

    def test_projection_not_tied_to_one_fixture(self):
        self.assertNotIn('考文垂', PROJECTION)
        self.assertNotIn('纽卡斯尔联', PROJECTION)
        self.assertIn('labels[2] === home', PROJECTION)
        self.assertIn('labels[4] === away', PROJECTION)

    def test_archive_reconciles_without_creating_formal_sample(self):
        def row(values):
            return [{'text': v, 'visible': True, 'colspan': 1} for v in values]
        fixture = {'provider_event_id': '42', 'home': 'A', 'away': 'B', 'kickoff_display': '2026-10-17 22:00'}
        url = 'https://vip.titan007.com/changeDetail/handicap.aspx?id=42&companyID=3&l=0'
        company = {'company_id': '3', 'company_label': 'Crow*', 'opening': {
            'displayed_full_datetime': '2026-10-10 09:00', 'home_handicap': -0.5,
            'home_water_raw': '0.90', 'away_water_raw': '0.98'}}
        projection = {'table_found': True, 'rows': [
            row(['时间', '比分', 'A', '盘', 'B', '变化时间', '状态']),
            row(['2', '0-0', '0.88', '半球', '1.00', '10-17 22:02', '滚']),
            row(['', '', '0.90', '半球', '0.98', '10-10 09:00', '早'])]}
        archive = prepare_archive(projection, fixture, company,
                                  {'final_url': url, 'captured_at_utc': '2026-10-29T00:00:00Z'})
        self.assertEqual(archive['prematch_quote_count'], 1)
        self.assertEqual(archive['excluded_live_rows'], 1)
        self.assertEqual(archive['source_table_row_indices'], [0, 2])
        self.assertTrue(archive['provider_opening_reconciled'])
        self.assertTrue(archive['fixture_identity_verified'])
        self.assertFalse(archive['formal_observation'])
        self.assertFalse(archive['training_eligible'])
        self.assertFalse(archive['independent_fixture_mapping_verified'])
        self.assertIsNone(archive['rows'][1]['quote_time_utc'])


if __name__ == '__main__':
    unittest.main()
