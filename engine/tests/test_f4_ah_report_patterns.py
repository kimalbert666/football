"""Verify reporting uses saved prices and the correct calendar-month cohort."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import f4_ah as ah
from f4_ledger import save_outcome


class PriceVenueReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'asian_handicap': {
            'enabled': True, 'experiment_id': 'synthetic-report-only', 'research_only': True,
            'automatic_promotion': False, 'automatic_wagering': False,
            'windows': {'180': 20, '60': 10, '15': 5}, 'max_capture_age_minutes': 5,
            'bookmaker_priority': ['crown', 'hkjc'], 'started_at': '2026-09-01T00:00:00Z'},
            'notifications': {'policy': 'monthly_and_critical_only',
                              'started_at': '2026-09-01T00:00:00Z'}}
        ah.write(self.root / 'data/f4/config.json', self.config)

    def capture_and_settle(self, name, kickoff, book, line, prices, score):
        kickoff = ah.instant(kickoff)
        decision = kickoff - timedelta(minutes=15)
        fixture = {'match_id': 'synthetic-' + name, 'provider': 'espn',
                   'league': 'england-premier', 'home_id': name + '-home',
                   'away_id': name + '-away', 'home': name + ' Home', 'away': name + ' Away',
                   'kickoff_at': ah.stamp(kickoff), 'status': 'scheduled'}
        quote = {'bookmaker': book, 'market': '90min_asian_handicap',
                 'home_handicap': line, 'odds': prices, 'captured_at': ah.stamp(decision),
                 'published_at': ah.stamp(decision), 'provider_event_id': name,
                 'url': 'https://example.invalid/synthetic-only', 'content_sha256': 'a' * 64}
        ah.run_ah(self.root, [fixture], 'capture', clock=lambda: decision,
                  source=lambda *args, **kwargs: {
                      'markets': {fixture['match_id']: quote}, 'sources': [], 'problems': []})
        save_outcome(self.root, {**fixture, 'status': 'completed',
                                'home_score': score[0], 'away_score': score[1]},
                     ah.stamp(kickoff + timedelta(hours=3)))

    def review(self):
        return ah.run_ah(self.root, [], 'review',
                         clock=lambda: datetime(2026, 11, 1, 4, tzinfo=timezone.utc))

    def test_current_report_and_json_use_saved_price_and_venue(self):
        self.capture_and_settle('october', '2026-10-02T11:00:00Z', 'hkjc',
                                .25, [1.85, 1.98], [0, 2])
        summary = self.review()
        diagnostics = summary['price_venue_diagnostics']
        self.assertEqual(diagnostics['unique_fixtures'], 1)
        upper = next(g for g in diagnostics['groups'] if g['side'] == 'upper')
        self.assertEqual((upper['bookmaker'], upper['abs_handicap'], upper['venue']),
                         ('hkjc', .25, 'away'))
        self.assertEqual(upper['settlement_counts']['full_win'], 1)
        self.assertEqual(upper['records'][0]['decimal_odds'], 1.98)
        saved = ah.read(self.root / 'data/f4/ah/latest.json')
        self.assertEqual(saved['price_venue_diagnostics'], diagnostics)
        self.assertIn('1.90–2.00', ah.render_ah(summary))
        self.assertIn('不是当月或模型预测成绩', ah.render_ah(summary))
        self.assertEqual(ah.read(self.root / 'data/f4/config.json'), self.config)

    def test_monthly_report_does_not_pool_prior_month_prices(self):
        self.capture_and_settle('september', '2026-09-29T11:00:00Z', 'crown',
                                -1.25, [1.77, 2.10], [1, 0])
        self.capture_and_settle('october', '2026-10-02T11:00:00Z', 'hkjc',
                                .25, [1.85, 1.98], [0, 2])
        summary = self.review()
        self.assertEqual(summary['price_venue_diagnostics']['unique_fixtures'], 2)
        ah.archive_month(self.root, datetime(2026, 11, 1, 4, tzinfo=timezone.utc), summary)
        text = (self.root / 'data/f4/reports/monthly-2026-10.md').read_text(encoding='utf-8')
        self.assertIn('仅含2026-10开球', text)
        self.assertIn('公司：hkjc', text)
        self.assertNotIn('公司：crown', text)
        self.assertNotIn('september Home', text)
        self.assertIn('范围不同于上述当月统计', text)

    def test_hiding_cumulative_breakdown_preserves_other_summary_content(self):
        self.capture_and_settle('october', '2026-10-02T11:00:00Z', 'hkjc',
                                -1.25, [1.98, 1.84], [2, 1])
        summary = self.review()
        text = ah.render_ah(summary, include_price_venue=False)
        self.assertIn('T−15分钟已有赛果配对：1场', text)
        self.assertNotIn('1.98', text)
        self.assertNotIn('price-venue', text)


if __name__ == '__main__':
    unittest.main()
