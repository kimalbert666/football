import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from f4_scheduler import HEALTH_PATH, WORKERS, run

NOW = datetime(2026, 11, 10, 1, 30, tzinfo=timezone.utc)


class FakeAPI:
    def __init__(self, age=None, active=False, fail_dispatch=False):
        self.age, self.active, self.fail_dispatch = age, active, fail_dispatch
        self.calls, self.health = [], None

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if method == 'GET' and '/runs?' in path:
            return {'workflow_runs': [{'id': 9, 'status': 'queued'}] if self.active else []}
        if method == 'GET' and path.startswith('/contents/' + HEALTH_PATH):
            return {'sha': 'previous-health'}
        if method == 'GET' and path.startswith('/contents/'):
            if self.age is None:
                return None
            data = {'checked_at': (NOW-timedelta(seconds=self.age)).isoformat(),
                    'finished_at_utc': (NOW-timedelta(seconds=self.age)).isoformat()}
            return {'content': base64.b64encode(json.dumps(data).encode()).decode(), 'sha': 'checkpoint'}
        if method == 'POST':
            if self.fail_dispatch:
                raise RuntimeError('GitHub HTTP 422')
            return None
        if method == 'PUT':
            assert path == '/contents/' + HEALTH_PATH
            assert body['sha'] == 'previous-health' and body['branch'] == 'main'
            self.health = json.loads(base64.b64decode(body['content']))
            return {}
        raise AssertionError((method, path))


class SchedulerTests(unittest.TestCase):
    def check(self, api):
        health = run('owner/test', 'fake', now=NOW, request=api, run_id='42', event_name='schedule')
        self.assertEqual(health, api.health)
        self.assertEqual(health['trigger_event'], 'schedule')
        return health

    def test_missing_or_stale_checkpoint_requests_recovery_and_saves_evidence(self):
        for age in (None, 27*3600):
            api = FakeAPI(age)
            health = self.check(api)
            self.assertEqual([r['action'] for r in health['workers']], ['dispatch_requested']*2)
            posts = [c for c in api.calls if c[0] == 'POST']
            self.assertEqual(posts[0][2], {'ref': 'main', 'inputs': {'command': 'capture'}})
            self.assertEqual(posts[1][2], {'ref': 'main', 'inputs': {}})
            self.assertTrue(all(r['completion_verified'] is False for r in health['workers']))

    def test_fresh_or_active_workers_do_not_duplicate_work(self):
        for api, action in ((FakeAPI(60), 'skip_fresh'), (FakeAPI(active=True), 'skip_active')):
            health = self.check(api)
            self.assertEqual([r['action'] for r in health['workers']], [action]*2)
            self.assertFalse(any(c[0] == 'POST' for c in api.calls))

    def test_thresholds_are_independent_for_hourly_and_daily_workers(self):
        health = self.check(FakeAPI(76*60))
        self.assertEqual([r['action'] for r in health['workers']], ['dispatch_requested', 'skip_fresh'])

    def test_future_checkpoint_is_error_preserved_without_dispatch(self):
        api = FakeAPI(-1)
        health = self.check(api)
        self.assertEqual(health['status'], 'error')
        self.assertTrue(all(r['action'] == 'error' for r in health['workers']))
        self.assertFalse(any(c[0] == 'POST' for c in api.calls))

    def test_failed_dispatch_is_recorded_and_not_retried(self):
        api = FakeAPI(None, fail_dispatch=True)
        health = self.check(api)
        self.assertEqual(health['status'], 'error')
        self.assertEqual(len([c for c in api.calls if c[0] == 'POST']), 2)


if __name__ == '__main__':
    unittest.main()
