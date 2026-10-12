"""Synthetic contracts only: boundary, settlement and independence safeguards."""
import copy
import json
import unittest

from f4_ah_patterns import odds_band, price_venue_diagnostics, render_price_venue_diagnostics


def paired(match='synthetic:one', line=-.25, prices=(1.90, 1.85), score=(1, 0), book='hkjc', experiment='synthetic-v1'):
    return {'match_id': match, 'experiment_id': experiment, 'home_id': 'synthetic-home',
            'away_id': 'synthetic-away', 'home': 'Home', 'away': 'Away', 'horizon': 'T-15m',
            'observed_at': '2026-10-10T11:15:00Z', 'kickoff_at': '2026-10-10T11:30:00Z',
            'settled_at': '2026-10-10T13:30:00Z', 'home_handicap': line,
            'odds': list(prices), 'final_score': list(score), 'bookmaker': book,
            'quote': {'bookmaker': book, 'market': '90min_asian_handicap',
                      'home_handicap': line, 'odds': list(prices)}}


class PriceVenueDiagnosticsTests(unittest.TestCase):
    def test_exact_requested_boundaries_do_not_absorb_neighbouring_prices(self):
        expectations = {1.01: 'below_1_75', 1.74: 'below_1_75', 1.75: '1_75_to_1_85',
                        1.85: '1_75_to_1_85', 1.850001: 'above_1_85_below_1_90',
                        1.89: 'above_1_85_below_1_90', 1.90: '1_90_to_2_00',
                        2.00: '1_90_to_2_00', 2.000001: 'above_2_00', 2.03: 'above_2_00'}
        for price, expected in expectations.items():
            with self.subTest(price=price):
                self.assertEqual(odds_band(price), expected)
        for price in (1, True, float('nan'), float('inf'), '1.90'):
            with self.subTest(invalid=price), self.assertRaises(ValueError):
                odds_band(price)

    def test_upper_is_giving_team_not_home_or_lower_price(self):
        summary = price_venue_diagnostics([paired(line=.25, prices=(1.80, 2.03), score=(0, 2))])
        upper = next(group for group in summary['groups'] if group['side'] == 'upper')
        lower = next(group for group in summary['groups'] if group['side'] == 'lower')
        self.assertEqual((upper['venue'], upper['odds_band'], upper['settlement_counts']['full_win']),
                         ('away', 'above_2_00', 1))
        self.assertEqual(lower['venue'], 'home')
        self.assertEqual(upper['records'][0]['decimal_odds'], 2.03)

    def test_flat_push_does_not_become_upper_lower_or_positive_return(self):
        summary = price_venue_diagnostics([paired(line=0, score=(2, 2))])
        self.assertEqual({g['side'] for g in summary['groups']}, {'flat_home', 'flat_away'})
        self.assertEqual(summary['unique_fixtures'], 1)
        self.assertEqual(summary['side_records'], 2)
        for group in summary['groups']:
            self.assertEqual(group['settlement_counts']['push'], 1)
            self.assertEqual(group['positive_return_rate'], 0)
            self.assertEqual(group['unit_stake_roi'], 0)

    def test_five_classes_and_rates_have_correct_push_denominator(self):
        rows = [paired(str(i), line=-.25, score=score) for i, score in enumerate(((1, 0), (0, 0), (0, 1)))]
        rows += [paired('half-win', line=.25, score=(0, 0)), paired('push', line=0, score=(0, 0)),
                 paired('integer-push', line=-1, score=(1, 0))]
        summary = price_venue_diagnostics(rows)
        home_records = [record for group in summary['groups'] if group['venue'] == 'home' for record in group['records']]
        self.assertEqual({record['settlement'] for record in home_records},
                         {'full_win', 'half_win', 'push', 'half_loss', 'full_loss'})
        upper_band = next(g for g in summary['book_summaries'][0]['odds_band_groups']
                          if g['side'] == 'upper' and g['odds_band'] == '1_90_to_2_00')
        self.assertEqual(upper_band['matches'], 4)
        self.assertEqual(upper_band['positive_return_rate'], 1 / 4)
        lower_halfwin = next(g for g in summary['groups'] if g['side'] == 'lower' and g['venue'] == 'home')
        self.assertEqual(lower_halfwin['positive_return_rate'], 1)
        self.assertEqual(lower_halfwin['full_win_rate'], 0)

    def test_duplicate_uses_earliest_valid_snapshot_after_normalizing_timezone(self):
        later = paired(prices=(1.74, 2.10))
        later['observed_at'] = '2026-10-10T11:16:00Z'
        first = paired(prices=(1.90, 1.85))
        first['observed_at'] = '2026-10-10T19:15:00+08:00'
        invalid = paired(prices=(1.80, 2.00))
        invalid['observed_at'] = '2026-10-10T11:00:00Z'
        summary = price_venue_diagnostics([later, invalid, first])
        self.assertEqual(summary['accepted_pairs'], 1)
        self.assertEqual(summary['duplicate_pairs_excluded'], 1)
        self.assertEqual(len(summary['rejected_pairs']), 1)
        self.assertEqual(next(g for g in summary['groups'] if g['side'] == 'upper')['odds_band'], '1_90_to_2_00')

    def test_bookmakers_and_experiments_remain_separate_unique_match_is_one(self):
        rows = [paired(), paired(book='crown'), paired(experiment='synthetic-v2')]
        summary = price_venue_diagnostics(rows)
        self.assertEqual(summary['unique_fixtures'], 1)
        self.assertEqual(summary['accepted_pairs'], 3)
        self.assertEqual(len(summary['book_summaries']), 3)
        self.assertTrue(all(scope['paired_matches'] == 1 for scope in summary['book_summaries']))
        self.assertEqual(len(summary['groups']), 6)

    def test_wrong_window_timing_identity_quote_and_quarters_are_rejected(self):
        changes = [{'horizon': 'T-60m'}, {'observed_at': '2026-10-10T11:30:00Z'},
                   {'observed_at': '2026-10-10T11:09:59Z'}, {'settled_at': '2026-10-10T11:30:00Z'},
                   {'home_handicap': .30}, {'odds': [1.0, 2.0]}, {'final_score': [True, 0]},
                   {'away_id': 'synthetic-home'}, {'bookmaker': 'crown'}, {'experiment_id': ''},
                   {'quote': None}, {'quote': []}, {'quote': {}},
                   {'quote': {'bookmaker': 'hkjc'}},
                   {'quote': {'market': '90min_asian_handicap', 'home_handicap': -.25, 'odds': [1.90, 1.85]}},
                   {'quote': {'bookmaker': 'hkjc', 'home_handicap': -.25, 'odds': [1.90, 1.85]}},
                   {'quote': {'bookmaker': 'hkjc', 'home_handicap': -1.25}},
                   {'quote': {'bookmaker': 'hkjc', 'odds': [1.8, 2.0]}},
                   {'quote': {'bookmaker': 'hkjc', 'market': 'first_half_asian_handicap'}}]
        for change in changes:
            with self.subTest(change=change):
                summary = price_venue_diagnostics([{**paired(), **change}])
                self.assertEqual(summary['accepted_pairs'], 0)
                self.assertEqual(len(summary['rejected_pairs']), 1)

    def test_all_quarter_lines_and_half_settlement_net_profit(self):
        summary = price_venue_diagnostics([paired(line=-1.25, prices=(1.98, 1.84), score=(2, 1)),
                                          paired('deep', line=-5.75, score=(6, 0))])
        self.assertEqual(summary['accepted_pairs'], 2)
        upper = next(g for g in summary['groups'] if g['abs_handicap'] == 1.25 and g['side'] == 'upper')
        lower = next(g for g in summary['groups'] if g['abs_handicap'] == 1.25 and g['side'] == 'lower')
        self.assertEqual(upper['settlement_counts']['half_loss'], 1)
        self.assertEqual(upper['unit_stake_net_profit'], -.5)
        self.assertAlmostEqual(lower['unit_stake_net_profit'], .42)

    def test_seven_match_fixture_describes_actual_requested_prices_without_claiming_model_results(self):
        # Synthetic identity and timing; contracts mirror the user's seven-match counts.
        rows = [paired('arsenal', -1.25, (1.98, 1.84), (2, 1)),
                paired('brighton', .25, (1.80, 2.03), (0, 2)),
                paired('fulham', .25, (1.83, 1.99), (2, 1)),
                paired('chelsea', -.75, (1.96, 1.86), (5, 1)),
                paired('villa', 0, (1.86, 1.96), (2, 2)),
                paired('united', -.75, (1.80, 2.03), (1, 1)),
                paired('city', .25, (2.11, 1.74), (0, 3))]
        source = copy.deepcopy(rows)
        summary = price_venue_diagnostics(rows)
        self.assertEqual(rows, source)
        self.assertEqual(summary['unique_fixtures'], 7)
        upper = {g['odds_band']: g for g in summary['book_summaries'][0]['odds_band_groups'] if g['side'] == 'upper'}
        self.assertEqual(upper['1_75_to_1_85']['settlement_counts']['full_loss'], 1)
        self.assertEqual(upper['1_90_to_2_00']['matches'], 3)
        self.assertEqual(upper['1_90_to_2_00']['settlement_counts'],
                         {'full_win': 1, 'half_win': 0, 'push': 0, 'half_loss': 1, 'full_loss': 1})
        self.assertEqual(upper['below_1_75']['matches'], 1)
        self.assertEqual(upper['above_2_00']['matches'], 1)
        self.assertNotIn('above_1_85_below_1_90', upper)
        json.dumps(summary, allow_nan=False)
        markdown = render_price_venue_diagnostics(summary)
        self.assertIn('| 上盘 | 1.75–1.85 | 1 | 0/0/0/0/1 |', markdown)
        self.assertIn('| 上盘 | 1.90–2.00 | 3 | 1/0/0/1/1 |', markdown)
        self.assertIn('| 0.25 | 上盘 | 客场 | >2.00 | 1 |', markdown)
        self.assertIn('不是模型命中成绩', markdown)

    def test_empty_input_reports_no_qualified_samples(self):
        summary = price_venue_diagnostics([])
        self.assertEqual(summary['unique_fixtures'], 0)
        self.assertEqual(summary['groups'], [])
        self.assertIn('尚无符合固定T-15', render_price_venue_diagnostics(summary))
