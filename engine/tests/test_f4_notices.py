"""Research notifications: delivery ACKs, HK month boundaries, and fault episodes."""
import copy
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from f4_notices import build_notice_policy


class NoticePolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = {'experiment_id': 'synthetic-ah', 'notifications': {
            'started_at': '2026-09-23T00:00:00+08:00', 'timezone': 'Asia/Hong_Kong'}}
        self.fault = {'critical_incidents': [{'code': 'fixture_source_unavailable',
                       'source': 'fixture-feed', 'detail': 'request failed'}]}
        self.reports = {'2026-08': 'a' * 64, '2026-09': 'b' * 64, '2026-10': 'c' * 64}

    def policy(self, now='2026-09-23T01:00:00+08:00', health=None, ack=None, reports=None, state=None):
        return build_notice_policy(self.config, now, health or {}, ack or {}, reports, state=state)

    def test_september_never_sends_august_report(self):
        result = self.policy(reports=self.reports)
        self.assertFalse(result['notify'])
        self.assertIsNone(result['month'])

    def test_hong_kong_month_boundary(self):
        before = self.policy('2026-09-30T15:59:59Z', reports=self.reports)
        after = self.policy('2026-09-30T16:00:00Z', reports=self.reports)
        self.assertFalse(before['notify'])
        self.assertTrue(after['notify'])
        self.assertEqual(after['month'], '2026-09')
        self.assertEqual(after['notice']['report_sha256'], 'b' * 64)

    def test_open_month_and_non_previous_month_not_sent(self):
        result = self.policy('2026-11-01T01:00:00+08:00', reports={'2026-09': 'a' * 64, '2026-11': 'b' * 64})
        self.assertFalse(result['notify'])
        self.assertEqual(result['reason'], 'monthly_report_not_available')

    def test_existing_archive_is_required_and_hash_must_be_valid(self):
        for reports in (None, {}, {'2026-09': True}, {'2026-09': 'not-a-sha'}, {'2026-09': 'z' * 64}):
            with self.subTest(reports=reports):
                self.assertFalse(self.policy('2026-10-01T00:01:00+08:00', reports=reports)['notify'])

    def test_monthly_failed_delivery_retries_same_token_and_notice(self):
        first = self.policy('2026-10-01T00:01:00+08:00', reports=self.reports)
        retry = self.policy('2026-10-02T00:01:00+08:00', reports=self.reports, state=first['state'])
        self.assertTrue(retry['notify'])
        self.assertEqual(first['notice'], retry['notice'])

    def test_monthly_success_ack_silences_remaining_month_even_if_report_changes(self):
        first = self.policy('2026-10-01T00:01:00+08:00', reports=self.reports)
        result = self.policy('2026-10-02T00:01:00+08:00', ack={'token': first['token']},
                             reports={'2026-09': 'd' * 64}, state=first['state'])
        self.assertFalse(result['notify'])
        self.assertEqual(result['state']['acknowledged_months'], ['2026-09'])

    def test_token_only_ack_is_enough_after_monthly_state_loss(self):
        first = self.policy('2026-10-01T00:01:00+08:00', reports=self.reports)
        result = self.policy('2026-10-02T00:01:00+08:00', ack={'token': first['token']}, reports=self.reports)
        self.assertFalse(result['notify'])

    def test_next_calendar_month_can_send_next_report(self):
        first = self.policy('2026-10-01T00:01:00+08:00', reports=self.reports)
        result = self.policy('2026-11-01T00:01:00+08:00', ack=first['notice'],
                             reports=self.reports, state=first['state'])
        self.assertTrue(result['notify'])
        self.assertEqual(result['month'], '2026-10')
        self.assertNotEqual(result['token'], first['token'])

    def test_december_does_not_enable_experimental_direction_notices(self):
        self.config['notifications']['acceptance_date'] = '2026-12-01'
        result = self.policy('2027-01-15T12:00:00+08:00', health={
            'selected': True, 'directions': ['home -1.25'], 'new_predictions': 12})
        self.assertFalse(result['notify'])

    def test_no_match_or_untrained_candidate_is_not_a_fault(self):
        result = self.policy(health={'problems': ['no fixtures', 'candidate not trained'],
            'coverage': {'complete': False}, 'critical_incidents': [
                {'code': 'no_due_fixtures', 'source': 'schedule'},
                {'code': 'candidate_untrained', 'source': 'trainer'},
                {'code': 'insufficient_samples', 'source': 'trainer'}]})
        self.assertFalse(result['notify'])
        self.assertEqual(result['critical_codes'], [])

    def test_explicit_fault_immediately_notifies(self):
        result = self.policy(health=self.fault)
        self.assertTrue(result['notify'])
        self.assertEqual(result['reason'], 'critical_incident')
        self.assertEqual(result['critical_codes'], ['fixture_source_unavailable'])

    def test_fault_failed_delivery_retries_despite_changing_error_detail(self):
        first = self.policy(health=self.fault)
        self.fault['critical_incidents'][0]['detail'] = 'request failed at a new timestamp'
        retry = self.policy('2026-09-23T02:00:00+08:00', health=self.fault, state=first['state'])
        self.assertEqual(retry['notice'], first['notice'])

    def test_persistent_acknowledged_fault_never_repeats(self):
        first = self.policy(health=self.fault)
        second = self.policy('2026-09-23T02:00:00+08:00', health=self.fault,
                             ack={'token': first['token']}, state=first['state'])
        later = self.policy('2026-09-29T02:00:00+08:00', health=self.fault,
                            ack={'token': first['token']}, state=second['state'])
        self.assertFalse(second['notify'])
        self.assertFalse(later['notify'])

    def test_recovery_silent_and_later_new_fault_notifies(self):
        first = self.policy(health=self.fault)
        recovered = self.policy('2026-09-23T02:00:00+08:00', ack=first['notice'], state=first['state'])
        self.assertFalse(recovered['notify'])
        again = self.policy('2026-09-25T01:00:00+08:00', health=self.fault,
                            ack=first['notice'], state=recovered['state'])
        self.assertTrue(again['notify'])
        self.assertNotEqual(first['token'], again['token'])

    def test_intermittent_fault_cooldown_prevents_mail_spam(self):
        first = self.policy(health=self.fault)
        recovered = self.policy('2026-09-23T02:00:00+08:00', ack=first['notice'], state=first['state'])
        again = self.policy('2026-09-23T03:00:00+08:00', health=self.fault,
                            ack=first['notice'], state=recovered['state'])
        self.assertFalse(again['notify'])
        recovered2 = self.policy('2026-09-23T04:00:00+08:00', ack=first['notice'], state=again['state'])
        again2 = self.policy('2026-09-23T05:00:00+08:00', health=self.fault,
                             ack=first['notice'], state=recovered2['state'])
        self.assertFalse(again2['notify'])
        after_cooldown = self.policy('2026-09-24T03:00:00+08:00', health=self.fault,
                                     ack=first['notice'], state=again2['state'])
        self.assertTrue(after_cooldown['notify'])

    def test_distinct_source_or_code_is_a_new_incident(self):
        first = self.policy(health=self.fault)
        next_fault = copy.deepcopy(self.fault)
        next_fault['critical_incidents'].append({'code': 'quote_schema_changed', 'source': 'crown-feed', 'detail': 'bad fields'})
        result = self.policy('2026-09-23T02:00:00+08:00', health=next_fault,
                             ack=first['notice'], state=first['state'])
        self.assertTrue(result['notify'])
        self.assertEqual(len(result['notice']['incidents']), 1)
        self.assertEqual(result['notice']['incidents'][0]['code'], 'quote_schema_changed')

    def test_fault_preempts_monthly_without_losing_report(self):
        monthly = self.policy('2026-10-01T00:00:00+08:00', reports=self.reports)
        fault = self.policy('2026-10-01T00:01:00+08:00', health=self.fault,
                            reports=self.reports, state=monthly['state'])
        self.assertEqual(fault['notice']['type'], 'critical_incident')
        after = self.policy('2026-10-01T00:02:00+08:00', health=self.fault, ack=fault['notice'],
                            reports=self.reports, state=fault['state'])
        self.assertEqual(after['token'], monthly['token'])

    def test_recovered_undelivered_fault_is_not_sent_as_current_fault(self):
        first = self.policy(health=self.fault)
        recovered = self.policy('2026-09-23T02:00:00+08:00', state=first['state'])
        self.assertFalse(recovered['notify'])

    def test_inputs_not_mutated(self):
        first = self.policy(health=self.fault)
        snapshot = copy.deepcopy(first['state'])
        self.policy('2026-09-23T02:00:00+08:00', health=self.fault, ack=first['notice'], state=first['state'])
        self.assertEqual(first['state'], snapshot)

    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):
            self.policy(datetime(2026, 10, 1))


if __name__ == '__main__':
    unittest.main()
