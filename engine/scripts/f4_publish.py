"""Deliver only a notice already approved by f4_notices, with durable deduplication."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from f4_cloud import read_json, write_json, outputs

ROOT = Path(__file__).resolve().parents[2]
TITLE = '[f4] 英超并行验证'


def github(root, *args):
    result = subprocess.run(['gh', *args], cwd=root, capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        # Do not print credential-bearing environment or remote command output.
        raise RuntimeError('GitHub notice operation failed; delivery remains unacknowledged')
    return result.stdout


def publish(root, *, client=github):
    if os.getenv('GITHUB_ACTIONS') != 'true':
        return False
    logdir = root / '.github/run-logs'
    delivery = read_json(logdir / 'f4-delivery.json', {})
    if delivery.get('notify') is not True:
        return False
    notice = read_json(logdir / 'f4-notify-token.json', {})
    marker = 'f4-notification:' + str(notice.get('token', ''))
    body_path = logdir / 'f4-issue.md'
    body = body_path.read_text(encoding='utf-8')
    if not notice.get('token') or delivery.get('marker') != marker or f'<!-- {marker} -->' not in body:
        raise ValueError('Notice token/body mismatch; refusing stale delivery')
    config = read_json(root / 'data/f4/config.json', {})
    if (config.get('notifications', {}).get('policy') == 'monthly_and_critical_only'
            and notice.get('type') not in ('monthly_report', 'critical_incident')):
        raise ValueError('Research policy forbids this notification type')
    issues = json.loads(client(root, 'issue', 'list', '--state', 'open', '--search',
                               TITLE + ' in:title', '--json', 'number,title'))
    matches = [item for item in issues if item.get('title') == TITLE]
    if len(matches) > 1:
        raise ValueError('Multiple f4 notice issues; refusing ambiguous destination')
    if matches:
        number = str(matches[0]['number'])
        issue = json.loads(client(root, 'issue', 'view', number, '--json', 'body,comments'))
        existing = [issue.get('body') or '', *[c.get('body') or '' for c in issue.get('comments', [])]]
        if not any(f'<!-- {marker} -->' in item for item in existing):
            client(root, 'issue', 'comment', number, '--body-file', str(body_path))
    else:
        client(root, 'issue', 'create', '--title', TITLE, '--body-file', str(body_path),
               '--assignee', os.environ['GITHUB_REPOSITORY_OWNER'])
    # Only a successful delivery or a remotely confirmed existing marker is ACKed.
    write_json(root / 'data/f4/status/notification.json', notice)
    write_json(logdir / 'f4-delivery.json', {**delivery, 'notify': False, 'acknowledged': True})
    outputs(notify=False)
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    publish(parser.parse_args().root)
