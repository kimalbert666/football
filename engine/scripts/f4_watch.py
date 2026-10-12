"""Bounded pre-match worker: stay awake near known fixtures, persist each capture.

This reduces dependence on each cron tick, but cannot guarantee GitHub runner
availability. No match in the arming horizon means immediate exit.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from f4_ah import instant, stamp, read, write, load_observations
from f4_cloud import run as cloud_run

ROOT = Path(__file__).resolve().parents[2]


def fixture_targets(root, now):
    cfg = read(root / 'data/f4/config.json', {}).get('asian_handicap', {})
    if not cfg.get('enabled'):
        return []
    valid, latest = set(), {}
    for row in load_observations(root):
        if row.get('experiment_id') != cfg['experiment_id']:
            continue
        key = (row['match_id'], stamp(row['kickoff_at']), row['horizon'])
        if row.get('quote'):
            valid.add(key)
        latest[key] = max(latest.get(key, instant(row['observed_at'])), instant(row['observed_at']))
    targets = []
    for path in (root / 'data/f4/fixtures').glob('*.json'):
        row = read(path)
        if (row.get('league') != 'england-premier' or row.get('status') != 'scheduled'
                or not row.get('home_id') or not row.get('away_id') or not row.get('kickoff_at')):
            continue
        kickoff = instant(row['kickoff_at'])
        for minutes, tolerance in cfg['windows'].items():
            key = (row['match_id'], stamp(kickoff), f'T-{minutes}m')
            target = kickoff - timedelta(minutes=int(minutes))
            closes = min(kickoff, target + timedelta(minutes=tolerance))
            if key in valid or now >= closes:
                continue
            # A transient missing quote must not exhaust the whole window.
            # The deadline and spacing bound requests; T15 retries each minute.
            gap = timedelta(minutes=1 if int(minutes) == 15 else 2)
            retry_at = latest.get(key, target - gap) + gap
            due = max(target, retry_at)
            if due < closes:
                targets.append({'key': key, 'at': due, 'nominal_at': target, 'closes_at': closes})
    return sorted(targets, key=lambda t: (t['at'], t['key']))


def checkpoint(root):
    """Called only in Actions with its existing checkout credential helper."""
    if os.getenv('GITHUB_ACTIONS') != 'true':
        return
    def git(*args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True)

    def reject_rebase_conflict():
        for state in ('rebase-merge', 'rebase-apply'):
            value = git('rev-parse', '--git-path', state).stdout.strip()
            path = Path(value)
            if (path if path.is_absolute() else root / path).exists():
                raise RuntimeError('f4 checkpoint has a Git conflict; retaining local records') from None

    # A retry may inherit an interrupted rebase. Never stage conflict markers,
    # send notices, or make another commit in that state.
    reject_rebase_conflict()
    # Publish pending monthly/critical notices promptly during a long worker;
    # the publisher retains the research policy and acknowledges only success.
    from f4_publish import publish
    try:
        publish(root)
    except Exception as exc:
        # A mail/Issue outage must not lose the independent match records.
        write(root / 'data/f4/status/notice-delivery-health.json', {
            'status': 'failed', 'error': type(exc).__name__,
            'checked_at': stamp(datetime.now(timezone.utc)), 'acknowledged': False})
        print('::warning::f4 Issue delivery failed; records will persist and delivery will retry.')
    else:
        health_path = root / 'data/f4/status/notice-delivery-health.json'
        if health_path.exists():
            write(health_path, {'status': 'ok', 'checked_at': stamp(datetime.now(timezone.utc))})
    git('config', 'user.name', 'github-actions[bot]')
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    git('add', '--', 'data/f4')
    changed = subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=root)
    if changed.returncode == 0:
        return
    if changed.returncode != 1:
        raise RuntimeError('Cannot inspect staged f4 records')
    git('commit', '-m', 'chore(f4): checkpoint pre-match worker records')
    for attempt in range(3):
        try:
            # Retry transient pull failures too, but never discard a conflict.
            git('pull', '--rebase', 'origin', 'main')
            git('push', 'origin', 'HEAD:main')
            return
        except subprocess.CalledProcessError:
            reject_rebase_conflict()
            if attempt == 2:
                raise RuntimeError('Could not persist f4 checkpoint') from None
            time.sleep(attempt + 1)


def dispatch_continuation():
    if os.getenv('GITHUB_ACTIONS') != 'true':
        return False
    import requests
    repo = os.environ['GITHUB_REPOSITORY']
    response = requests.post(f'https://api.github.com/repos/{repo}/actions/workflows/f4-shadow.yml/dispatches',
                             headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                                      'Accept': 'application/vnd.github+json'},
                             json={'ref': 'main', 'inputs': {'command': 'watch'}}, timeout=25,
                             allow_redirects=False)
    if response.status_code != 204:
        raise RuntimeError(f'Worker continuation rejected: HTTP {response.status_code}')
    return True


def watch(root, *, max_minutes=300, arm_minutes=120, clock=lambda: datetime.now(timezone.utc),
          sleeper=time.sleep, run=None, persist=checkpoint, dispatch=dispatch_continuation):
    if not 0 <= max_minutes <= 300 or not 0 <= arm_minutes <= 180:
        raise ValueError('worker duration/arming exceeds configured bounds')
    started = clock()
    deadline = started + timedelta(minutes=max_minutes)
    prior = read(root / 'data/f4/status/watch-latest.json', {})
    status = {'started_at': stamp(started), 'run_id': os.getenv('GITHUB_RUN_ID'), 'captures': [],
              'mode': 'bounded_match_window_worker', 'timing_guaranteed': False,
              'previous_worker_started_at': prior.get('started_at'), 'continuation_requested': False}

    def save(state):
        status.update(state=state, checked_at=stamp(clock()))
        write(root / 'data/f4/status/watch-latest.json', status)

    def early_capture_checkpoint(path):
        # Commit the AH snapshot before unrelated legacy requests can delay or
        # fail the rest of the capture. Injected test runners stay independent.
        save('ah_saved_legacy_pending')
        persist(path)

    if run is None:
        def run(path, command, *, clock):
            return cloud_run(path, command, clock=clock,
                             capture_checkpoint=early_capture_checkpoint)

    # Immediate normal capture also refreshes today's/tomorrow's ESPN calendar.
    # Do this before slower result backfills so an already due window is first.
    save('refreshing_calendar')
    for attempt in range(3):
        try:
            run(root, 'capture', clock=clock)
            status.pop('last_error', None)
            break
        except Exception as exc:
            status['last_error'] = type(exc).__name__
            exhausted = attempt == 2 or clock() >= deadline
            save('failed' if exhausted else 'retrying_initial_capture')
            persist(root)
            if exhausted:
                raise
            sleeper(min(60, (deadline - clock()).total_seconds()))
    save('planning')
    persist(root)
    targets = fixture_targets(root, clock())
    # Queued cron ticks can replace daily/monthly ticks while a worker is alive.
    # Catch up once when it cannot displace a window in the next ten minutes.
    settled = read(root / 'data/f4/status/last-settlement.json', {})
    if ((not settled.get('checked_at') or clock() - instant(settled['checked_at']) >= timedelta(hours=20))
            and (not targets or targets[0]['at'] > clock() + timedelta(minutes=10))):
        save('catching_up_settlement')
        run(root, 'daily', clock=clock)
        status['settlement_catchup_at'] = stamp(clock())
        save('planning')
        persist(root)
        targets = fixture_targets(root, clock())
    if not targets or targets[0]['at'] > clock() + timedelta(minutes=arm_minutes):
        save('no_nearby_unrecorded_window')
        persist(root)
        return status
    last_refresh = clock()
    failures = 0
    while clock() < deadline:
        now = clock()
        targets = fixture_targets(root, now)
        if not targets or targets[0]['at'] > now + timedelta(minutes=arm_minutes):
            save('complete_or_no_nearby_window')
            break
        next_at = targets[0]['at']
        status['next_window_at'] = stamp(next_at)
        due = next_at <= now
        if due or now - last_refresh >= timedelta(minutes=30):
            # Re-fetch on each decision; cached fixtures only plan wake times.
            save('capturing' if due else 'refreshing_calendar')
            try:
                run(root, 'capture', clock=clock)
                last_refresh = clock()
                failures = 0
                status.pop('last_error', None)
                if due:
                    status['captures'].append({'requested_at': stamp(now),
                        'target_at': stamp(targets[0]['nominal_at']),
                        'delay_seconds': (now - targets[0]['nominal_at']).total_seconds(),
                        'completed_at': stamp(clock())})
                save('waiting')
                persist(root)
            except Exception as exc:
                failures += 1
                status['last_error'] = type(exc).__name__
                save('retrying' if failures < 3 else 'failed')
                persist(root)
                if failures >= 3:
                    raise
            # Bound retries even when an upstream exception produced no observation.
            sleeper(min(60, max(0, (deadline - clock()).total_seconds())))
        else:
            sleeper(min(60, (next_at - now).total_seconds(), (deadline - now).total_seconds()))
    if clock() >= deadline:
        remaining = fixture_targets(root, clock())
        if max_minutes > 0 and remaining and remaining[0]['at'] <= clock() + timedelta(minutes=arm_minutes):
            save('checkpoint_before_handoff')
            persist(root)
            status['continuation_requested'] = dispatch()
        save('duration_limit')
    persist(root)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--max-minutes', type=float, default=300)
    args = parser.parse_args()
    watch(args.root, max_minutes=args.max_minutes)


if __name__ == '__main__':
    main()
