"""Bounded scheduler health check and repository-local recovery requests."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HEALTH_PATH = 'data/f4/status/scheduler-health.json'
ACTIVE = {'queued', 'in_progress', 'waiting', 'requested', 'pending'}
WORKERS = (
    ('f4-shadow.yml', 'data/f4/status/watch-latest.json', 'checked_at', 75 * 60, {'command': 'capture'}),
    ('f4-titan-history.yml', 'data/f4/titan-history/status.json', 'finished_at_utc', 26 * 3600, {}),
)


def instant(value):
    if not isinstance(value, str):
        raise ValueError('checkpoint timestamp missing')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('checkpoint timestamp requires timezone')
    return result.astimezone(timezone.utc)


class Client:
    def __init__(self, repo, token, request=None):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise ValueError('repository identity invalid')
        self.repo, self.token, self.request = repo, token, request

    def call(self, method, path, body=None):
        if self.request:
            return self.request(method, path, body)
        headers = {'Authorization': 'Bearer ' + self.token,
                   'Accept': 'application/vnd.github+json', 'User-Agent': 'f4-scheduler-health'}
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers['Content-Type'] = 'application/json'
        url = 'https://api.github.com/repos/' + self.repo + path
        try:
            with urlopen(Request(url, data=data, headers=headers, method=method), timeout=20) as response:
                payload = response.read()
                return json.loads(payload) if payload else None
        except HTTPError as exc:
            if method == 'GET' and exc.code == 404 and path.startswith('/contents/'):
                return None
            raise RuntimeError('GitHub HTTP ' + str(exc.code)) from None
        except (URLError, TimeoutError):
            raise RuntimeError('GitHub network request failed') from None

    def content(self, path):
        record = self.call('GET', '/contents/' + path + '?ref=main')
        if record is None:
            return None
        return json.loads(base64.b64decode(record['content']))

    def save_health(self, health):
        previous = self.call('GET', '/contents/' + HEALTH_PATH + '?ref=main')
        body = {'message': 'chore(f4): record scheduler health', 'branch': 'main',
                'content': base64.b64encode((json.dumps(health, ensure_ascii=False, indent=2) + '\n').encode()).decode()}
        if previous is not None:
            body['sha'] = previous['sha']
        self.call('PUT', '/contents/' + HEALTH_PATH, body)


def run(repo, token, *, now=None, request=None, run_id=None, event_name=None):
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError('health check requires timezone')
    client = Client(repo, token, request)
    health = {'checked_at': now.astimezone(timezone.utc).isoformat(), 'repository': repo,
              'run_id': run_id, 'trigger_event': event_name, 'workers': [], 'status': 'ok'}
    for workflow, path, time_key, max_age, inputs in WORKERS:
        record = {'workflow': workflow, 'checkpoint_path': path}
        health['workers'].append(record)
        try:
            runs = client.call('GET', '/actions/workflows/' + workflow + '/runs?branch=main&per_page=20')['workflow_runs']
            active = [r['id'] for r in runs if r.get('status') in ACTIVE]
            if active:
                record.update(action='skip_active', active_run_ids=active)
                continue
            checkpoint = client.content(path)
            if checkpoint is not None:
                checked = instant(checkpoint.get(time_key))
                age = (now - checked).total_seconds()
                record.update(checkpoint_at=checked.isoformat(), age_seconds=age)
                if age < 0:
                    raise ValueError('checkpoint timestamp is in the future')
                if age <= max_age:
                    record['action'] = 'skip_fresh'
                    continue
            client.call('POST', '/actions/workflows/' + workflow + '/dispatches', {'ref': 'main', 'inputs': inputs})
            record.update(action='dispatch_requested', completion_verified=False)
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            record.update(action='error', error=str(exc))
            health['status'] = 'error'
    client.save_health(health)
    return health


if __name__ == '__main__':
    result = run(os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_TOKEN'],
                 run_id=os.environ.get('GITHUB_RUN_ID'), event_name=os.environ.get('GITHUB_EVENT_NAME'))
    print(json.dumps(result, ensure_ascii=False))
    # Operational failures stay explicit in the saved health record. Repeated
    # hourly API failures must not create a stream of failure emails.
    # Unexpected code errors and inability to save health still fail the job.
    raise SystemExit(0)
