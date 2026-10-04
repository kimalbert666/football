"""Offline delivery reliability tests; never create real issues."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from f4_cloud import read_json, write_json
from f4_publish import publish, TITLE


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_REPOSITORY_OWNER', 'synthetic-owner')
    monkeypatch.delenv('GITHUB_OUTPUT', raising=False)
    logdir = tmp_path / '.github/run-logs'
    write_json(tmp_path / 'data/f4/config.json', {'notifications': {'policy': 'monthly_and_critical_only'}})
    write_json(logdir / 'f4-delivery.json', {'notify': True, 'marker': 'f4-notification:test'})
    write_json(logdir / 'f4-notify-token.json', {'token': 'test', 'type': 'critical_incident'})
    (logdir / 'f4-issue.md').write_text('Synthetic fault\n<!-- f4-notification:test -->\n', encoding='utf-8')
    return tmp_path


def test_delivery_failure_retries_then_deduplicates_remote_marker(delivery):
    calls = []
    remote_bodies = []
    fail = [True]
    def client(root, *args):
        calls.append(args)
        if args[1] == 'list':
            return json.dumps([{'title': TITLE, 'number': 1}])
        if args[1] == 'view':
            return json.dumps({'body': '', 'comments': [{'body': b} for b in remote_bodies]})
        if args[1] == 'comment':
            # Simulate remote accepted the comment but the response was lost.
            remote_bodies.append((root / '.github/run-logs/f4-issue.md').read_text(encoding='utf-8'))
            if fail.pop():
                raise RuntimeError('connection lost')
        return ''
    with pytest.raises(RuntimeError):
        publish(delivery, client=client)
    assert not (delivery / 'data/f4/status/notification.json').exists()
    assert publish(delivery, client=client)
    assert len(remote_bodies) == 1
    assert read_json(delivery / 'data/f4/status/notification.json', {})['token'] == 'test'
    before = len(calls)
    assert not publish(delivery, client=client)
    assert len(calls) == before


def test_create_ack_and_policy_rejection(delivery):
    calls = []
    def client(root, *args):
        calls.append(args)
        return '[]' if args[1] == 'list' else ''
    write_json(delivery / '.github/run-logs/f4-notify-token.json', {'token': 'test', 'type': 'direction'})
    with pytest.raises(ValueError, match='policy'):
        publish(delivery, client=client)
    assert calls == []
    write_json(delivery / '.github/run-logs/f4-notify-token.json', {'token': 'test', 'type': 'monthly_report'})
    assert publish(delivery, client=client)
    assert calls[-1][1] == 'create'
    assert '--body-file' in calls[-1]


def test_mismatch_local_and_silent_never_send(delivery, monkeypatch):
    def never(*args):
        pytest.fail('must not call GitHub')
    monkeypatch.delenv('GITHUB_ACTIONS')
    assert not publish(delivery, client=never)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    write_json(delivery / '.github/run-logs/f4-delivery.json', {'notify': False})
    assert not publish(delivery, client=never)
    write_json(delivery / '.github/run-logs/f4-delivery.json', {'notify': True, 'marker': 'wrong'})
    with pytest.raises(ValueError, match='mismatch'):
        publish(delivery, client=never)
