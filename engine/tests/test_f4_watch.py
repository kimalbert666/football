"""Synthetic clocks/fixtures only: no requests, Git operations or real waits."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
import os

import pytest

import f4_watch as worker
from f4_ah import instant, stamp, write, read, stage, load_observations


class FakeClock:
    def __init__(self, now):
        self.now = instant(now)
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert 0 < seconds <= 60, 'worker must yield at least every minute'
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def setup(tmp_path):
    cfg = {'enabled': True, 'experiment_id': 'synthetic-watch', 'windows': {'15': 5}}
    write(tmp_path / 'data/f4/config.json', {'asian_handicap': cfg})
    fixture = {'match_id': 'espn:synthetic-watch', 'league': 'england-premier',
               'home_id': 'synthetic-home', 'away_id': 'synthetic-away',
               'kickoff_at': '2026-10-10T11:00:00Z', 'status': 'scheduled'}
    write(tmp_path / 'data/f4/fixtures/synthetic.json', fixture)
    return tmp_path, cfg, fixture


def observation(root, cfg, fixture, at, quote=None, horizon='T-15m', experiment=None):
    rows = load_observations(root)
    row = {'match_id': fixture['match_id'], 'kickoff_at': fixture['kickoff_at'],
           'experiment_id': experiment or cfg['experiment_id'], 'horizon': horizon,
           'observed_at': stamp(at), 'quote': quote}
    write(root / 'data/f4/ah/observations/synthetic' / f'{len(rows)}.json', row)


def capture_stub(root, cfg, fixture, *, missing=False):
    calls = []

    def run(path, command, *, clock):
        assert path == root
        calls.append((command, clock()))
        if command == 'daily':
            write(root / 'data/f4/status/last-settlement.json', {'checked_at': stamp(clock())})
            return
        current = read(root / 'data/f4/fixtures/synthetic.json')
        horizon = stage(current, clock(), cfg)
        if horizon and not any(row.get('quote') and row['kickoff_at'] == current['kickoff_at']
                               and row['horizon'] == horizon for row in load_observations(root)):
            observation(root, cfg, current, clock(), None if missing else {'synthetic': True}, horizon)
    return run, calls


def test_targets_ignore_disabled_other_leagues_and_finished_windows(setup):
    root, cfg, fixture = setup
    now = instant('2026-10-10T10:40:00Z')
    assert len(worker.fixture_targets(root, now)) == 1
    for change in ({'status': 'completed'}, {'league': 'fifa-world-cup'}, {'home_id': None}):
        write(root / 'data/f4/fixtures/synthetic.json', {**fixture, **change})
        assert worker.fixture_targets(root, now) == []
    write(root / 'data/f4/fixtures/synthetic.json', fixture)
    assert worker.fixture_targets(root, instant('2026-10-10T10:50:00Z')) == []
    write(root / 'data/f4/config.json', {'asian_handicap': {**cfg, 'enabled': False}})
    assert worker.fixture_targets(root, now) == []


def test_retry_spacing_deadline_and_experiment_identity(setup):
    root, cfg, fixture = setup
    now = instant('2026-10-10T10:45:00Z')
    observation(root, cfg, fixture, now, {'old': True}, experiment='old-experiment')
    assert worker.fixture_targets(root, now)[0]['at'] == now
    observation(root, cfg, fixture, now)
    assert worker.fixture_targets(root, now)[0]['at'] == now + timedelta(minutes=1)
    observation(root, cfg, fixture, now + timedelta(minutes=1))
    assert worker.fixture_targets(root, now)[0]['at'] == now + timedelta(minutes=2)
    observation(root, cfg, fixture, now + timedelta(minutes=2))
    assert worker.fixture_targets(root, now)[0]['at'] == now + timedelta(minutes=3)
    observation(root, cfg, fixture, now + timedelta(minutes=4))
    assert worker.fixture_targets(root, now) == []


def test_changed_kickoff_never_reuses_old_capture(setup):
    root, cfg, fixture = setup
    now = instant('2026-10-10T10:45:00Z')
    observation(root, cfg, fixture, now, {'synthetic': True})
    assert worker.fixture_targets(root, now) == []
    moved = {**fixture, 'kickoff_at': '2026-10-10T12:00:00Z'}
    write(root / 'data/f4/fixtures/synthetic.json', moved)
    assert worker.fixture_targets(root, now)[0]['at'] == instant('2026-10-10T11:45:00Z')


def test_no_matches_nearby_exits_without_waiting_and_catches_up_once(setup):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-04T10:00:00Z')
    run, calls = capture_stub(root, cfg, fixture)
    persist, dispatch = Mock(), Mock(return_value=True)
    status = worker.watch(root, clock=clock, sleeper=clock.sleep, run=run, persist=persist, dispatch=dispatch)
    assert status['state'] == 'no_nearby_unrecorded_window'
    assert [cmd for cmd, _ in calls] == ['capture', 'daily']
    assert not clock.sleeps
    dispatch.assert_not_called()
    assert persist.call_count >= 2
    assert status['timing_guaranteed'] is False


@pytest.mark.parametrize('age_hours,expected_daily', [(19.99, False), (20, True)])
def test_settlement_catchup_exact_cutoff(setup, age_hours, expected_daily):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-04T10:00:00Z')
    write(root / 'data/f4/status/last-settlement.json',
          {'checked_at': stamp(clock() - timedelta(hours=age_hours))})
    run, calls = capture_stub(root, cfg, fixture)
    worker.watch(root, clock=clock, sleeper=clock.sleep, run=run, persist=Mock(), dispatch=Mock())
    assert sum(command == 'daily' for command, _ in calls) == int(expected_daily)


def test_worker_holds_runner_and_captures_target_without_cron(setup):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-10T10:05:00Z')
    run, calls = capture_stub(root, cfg, fixture)
    dispatch = Mock(return_value=True)
    status = worker.watch(root, clock=clock, sleeper=clock.sleep, run=run, persist=Mock(), dispatch=dispatch)
    assert status['state'] == 'complete_or_no_nearby_window'
    assert status['captures'][0]['target_at'] == '2026-10-10T10:45:00Z'
    assert status['captures'][0]['delay_seconds'] == 0
    records = load_observations(root)
    assert len(records) == 1
    assert records[0]['observed_at'] == '2026-10-10T10:45:00Z'
    assert any(command == 'capture' and at == instant('2026-10-10T10:35:00Z') for command, at in calls)
    assert max(clock.sleeps) <= 60
    dispatch.assert_not_called()


def test_default_arming_fits_single_fixture_all_windows_in_one_worker(setup):
    root, cfg, fixture = setup
    cfg['windows'] = {'180': 20, '60': 10, '15': 5}
    write(root / 'data/f4/config.json', {'asian_handicap': cfg})
    # Exactly two hours before T180: all three observations fit within 300m.
    clock = FakeClock('2026-10-10T06:00:00Z')
    run, calls = capture_stub(root, cfg, fixture)
    dispatch = Mock(return_value=True)
    status = worker.watch(root, clock=clock, sleeper=clock.sleep, run=run,
                          persist=Mock(), dispatch=dispatch)
    assert {row['horizon'] for row in load_observations(root)} == {'T-180m', 'T-60m', 'T-15m'}
    assert clock() < instant('2026-10-10T11:00:00Z')
    assert status['state'] == 'complete_or_no_nearby_window'
    dispatch.assert_not_called()


def test_default_arming_does_not_hold_three_hours_before_first_target(setup):
    root, cfg, fixture = setup
    cfg['windows'] = {'180': 20, '60': 10, '15': 5}
    write(root / 'data/f4/config.json', {'asian_handicap': cfg})
    clock = FakeClock('2026-10-10T05:00:00Z')
    run, calls = capture_stub(root, cfg, fixture)
    status = worker.watch(root, clock=clock, sleeper=clock.sleep, run=run,
                          persist=Mock(), dispatch=Mock())
    assert status['state'] == 'no_nearby_unrecorded_window'
    assert not load_observations(root)
    assert not clock.sleeps


def test_calendar_refresh_can_cancel_target_without_phantom_capture(setup):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-10T10:05:00Z')
    run, calls = capture_stub(root, cfg, fixture)

    def postpone(path, command, *, clock):
        if command == 'capture' and clock() >= instant('2026-10-10T10:35:00Z'):
            write(root / 'data/f4/fixtures/synthetic.json', {**fixture, 'status': 'postponed'})
        run(path, command, clock=clock)
    status = worker.watch(root, clock=clock, sleeper=clock.sleep, run=postpone, persist=Mock(), dispatch=Mock())
    assert status['state'] == 'complete_or_no_nearby_window'
    assert not status['captures']
    assert load_observations(root) == []


def test_missing_quotes_retry_until_deadline_and_never_spin_after_window(setup):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-10T10:45:00Z')
    run, calls = capture_stub(root, cfg, fixture, missing=True)
    status = worker.watch(root, clock=clock, sleeper=clock.sleep, run=run, persist=Mock(), dispatch=Mock())
    assert status['state'] == 'complete_or_no_nearby_window'
    records = load_observations(root)
    assert [row['observed_at'] for row in records] == [
        '2026-10-10T10:45:00Z', '2026-10-10T10:46:00Z',
        '2026-10-10T10:47:00Z', '2026-10-10T10:48:00Z', '2026-10-10T10:49:00Z']
    assert not any(row['quote'] for row in records)
    assert clock() < instant(fixture['kickoff_at'])


def test_source_recovers_after_three_early_failures_before_window_closes(setup):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-10T10:40:00Z')
    calls = []

    def temporary_missing(path, command, *, clock):
        calls.append((command, clock()))
        if command != 'capture':
            return
        horizon = stage(fixture, clock(), cfg)
        if horizon:
            quote = {'synthetic': True} if clock() >= instant('2026-10-10T10:49:00Z') else None
            observation(root, cfg, fixture, clock(), quote, horizon)

    worker.watch(root, clock=clock, sleeper=clock.sleep, run=temporary_missing,
                 persist=Mock(), dispatch=Mock())
    rows = load_observations(root)
    assert len([r for r in rows if not r['quote']]) > 3
    valid = [r for r in rows if r['quote']]
    assert len(valid) == 1
    assert valid[0]['observed_at'] == '2026-10-10T10:49:00Z'
    assert all(instant(r['observed_at']) < instant('2026-10-10T10:50:00Z') for r in rows)


def test_initial_transient_capture_exception_retries_with_real_clock(setup):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-10T10:45:00Z')
    base, calls = capture_stub(root, cfg, fixture)
    attempts = []

    def temporary_failure(path, command, *, clock):
        attempts.append(clock())
        if len(attempts) == 1:
            raise TimeoutError('synthetic initial timeout')
        base(path, command, clock=clock)

    status = worker.watch(root, clock=clock, sleeper=clock.sleep, run=temporary_failure,
                          persist=Mock(), dispatch=Mock())
    assert attempts[:2] == [instant('2026-10-10T10:45:00Z'), instant('2026-10-10T10:46:00Z')]
    assert load_observations(root)[0]['observed_at'] == '2026-10-10T10:46:00Z'
    assert 'last_error' not in status


def test_initial_permanent_capture_failure_stops_after_three_attempts(setup):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-10T10:45:00Z')
    failing = Mock(side_effect=TimeoutError('synthetic ongoing timeout'))
    with pytest.raises(TimeoutError):
        worker.watch(root, clock=clock, sleeper=clock.sleep, run=failing,
                     persist=Mock(), dispatch=Mock())
    assert failing.call_count == 3
    assert read(root / 'data/f4/status/watch-latest.json')['state'] == 'failed'


def test_default_cloud_adapter_persists_ah_before_legacy_tail(setup, monkeypatch):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-10T10:45:00Z')
    events = []
    base, calls = capture_stub(root, cfg, fixture)

    def cloud(path, command, *, clock, capture_checkpoint):
        base(path, command, clock=clock)
        if command == 'capture':
            capture_checkpoint(path)
            events.append(('legacy', len(load_observations(path))))

    def persist(path):
        state = read(path / 'data/f4/status/watch-latest.json')['state']
        if state == 'ah_saved_legacy_pending':
            events.append(('persist_ah', len(load_observations(path))))

    monkeypatch.setattr(worker, 'cloud_run', cloud)
    worker.watch(root, clock=clock, sleeper=clock.sleep, persist=persist, dispatch=Mock())
    assert events[:2] == [('persist_ah', 1), ('legacy', 1)]


def test_deadline_persists_before_single_continuation(setup):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-10T10:00:00Z')
    run, calls = capture_stub(root, cfg, fixture)
    events = []
    persist = lambda path: events.append(('persist', read(path / 'data/f4/status/watch-latest.json')['state']))

    def dispatch():
        events.append(('dispatch', None))
        return True
    status = worker.watch(root, max_minutes=10, clock=clock, sleeper=clock.sleep,
                          run=run, persist=persist, dispatch=dispatch)
    assert clock() == instant('2026-10-10T10:10:00Z')
    assert status['state'] == 'duration_limit'
    assert status['continuation_requested'] is True
    assert events[-3:] == [('persist', 'checkpoint_before_handoff'), ('dispatch', None), ('persist', 'duration_limit')]
    assert not load_observations(root)


def test_fetch_that_crosses_window_is_never_replayed_with_old_clock(setup):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-10T10:44:00Z')
    run, calls = capture_stub(root, cfg, fixture)

    def slow(path, command, *, clock):
        if command == 'capture':
            clock.now += timedelta(minutes=20)
        run(path, command, clock=clock)
    status = worker.watch(root, clock=clock, sleeper=clock.sleep, run=slow, persist=Mock(), dispatch=Mock())
    assert status['state'] == 'no_nearby_unrecorded_window'
    assert load_observations(root) == []
    assert not clock.sleeps


def test_repeated_capture_exceptions_stop_after_three_retries(setup):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-10T10:44:00Z')
    run, calls = capture_stub(root, cfg, fixture, missing=True)
    failures = []

    def failing(path, command, *, clock):
        if command == 'capture' and clock() >= instant('2026-10-10T10:46:00Z'):
            failures.append(clock())
            raise ValueError('synthetic upstream failure')
        run(path, command, clock=clock)
    with pytest.raises(ValueError, match='synthetic upstream'):
        worker.watch(root, clock=clock, sleeper=clock.sleep, run=failing, persist=Mock(), dispatch=Mock())
    assert len(failures) == 3
    assert read(root / 'data/f4/status/watch-latest.json')['state'] == 'failed'


@pytest.mark.parametrize('field,value', [('max_minutes', -1), ('max_minutes', 301),
                                         ('arm_minutes', -1), ('arm_minutes', 181)])
def test_reject_unbounded_worker_before_side_effects(setup, field, value):
    root, _, _ = setup
    run = Mock()
    with pytest.raises(ValueError):
        worker.watch(root, run=run, **{field: value})
    run.assert_not_called()


def test_continuation_no_credentials_or_network_outside_actions(monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    with patch('requests.post') as post:
        assert worker.dispatch_continuation() is False
    post.assert_not_called()


def test_continuation_is_one_post_without_following_redirects(monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_REPOSITORY', 'synthetic/example')
    monkeypatch.setenv('GH_TOKEN', 'synthetic-test-only')
    with patch('requests.post', return_value=Mock(status_code=204)) as post:
        assert worker.dispatch_continuation() is True
    assert post.call_count == 1
    assert post.call_args.kwargs['allow_redirects'] is False
    assert post.call_args.kwargs['json']['inputs'] == {'command': 'watch'}


def test_checkpoint_does_nothing_outside_actions(monkeypatch, tmp_path):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    with patch.object(worker.subprocess, 'run') as run:
        worker.checkpoint(tmp_path)
    run.assert_not_called()


def test_failed_notice_does_not_prevent_record_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    with patch('f4_publish.publish', side_effect=RuntimeError('synthetic delivery failure')):
        with patch.object(worker.subprocess, 'run', return_value=Mock(returncode=0, stdout='.git/absent-state')) as run:
            worker.checkpoint(tmp_path)
    assert any(call.args[0] == ['git', 'add', '--', 'data/f4'] for call in run.call_args_list)
    assert read(tmp_path / 'data/f4/status/notice-delivery-health.json')['acknowledged'] is False


def test_checkpoint_retries_transient_pull_without_force_or_discard(monkeypatch, tmp_path):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    pulls, commands = [], []

    def git_run(args, **kwargs):
        commands.append(args)
        if args == ['git', 'diff', '--cached', '--quiet']:
            return Mock(returncode=1)
        if args == ['git', 'pull', '--rebase', 'origin', 'main']:
            pulls.append(args)
            if len(pulls) == 1:
                raise worker.subprocess.CalledProcessError(1, args)
        if args[:3] == ['git', 'rev-parse', '--git-path']:
            return Mock(returncode=0, stdout='.git/' + args[-1])
        return Mock(returncode=0)

    with patch('f4_publish.publish', return_value=False), patch.object(worker.subprocess, 'run', side_effect=git_run), \
            patch.object(worker.time, 'sleep') as sleep:
        worker.checkpoint(tmp_path)
    assert len(pulls) == 2
    sleep.assert_called_once_with(1)
    assert ['git', 'push', 'origin', 'HEAD:main'] in commands
    assert not any('--force' in args or 'reset' in args for args in commands)


def test_checkpoint_retains_conflicted_record_commit_without_retrying(monkeypatch, tmp_path):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    commands = []

    def git_run(args, **kwargs):
        commands.append(args)
        if args == ['git', 'diff', '--cached', '--quiet']:
            return Mock(returncode=1)
        if args == ['git', 'pull', '--rebase', 'origin', 'main']:
            (tmp_path / '.git/rebase-merge').mkdir(parents=True)
            raise worker.subprocess.CalledProcessError(1, args)
        if args[:3] == ['git', 'rev-parse', '--git-path']:
            return Mock(returncode=0, stdout='.git/' + args[-1])
        return Mock(returncode=0)

    with patch('f4_publish.publish', return_value=False), patch.object(worker.subprocess, 'run', side_effect=git_run), \
            patch.object(worker.time, 'sleep') as sleep, pytest.raises(RuntimeError, match='Git conflict'):
        worker.checkpoint(tmp_path)
    assert commands.count(['git', 'pull', '--rebase', 'origin', 'main']) == 1
    assert not any(args[1] in ('push', 'reset') for args in commands)
    sleep.assert_not_called()


def test_checkpoint_with_existing_rebase_never_stages_or_sends_notice(monkeypatch, tmp_path):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    (tmp_path / '.git/rebase-merge').mkdir(parents=True)

    def git_run(args, **kwargs):
        assert args[:3] == ['git', 'rev-parse', '--git-path']
        return Mock(returncode=0, stdout='.git/' + args[-1])

    with patch('f4_publish.publish') as publish, \
            patch.object(worker.subprocess, 'run', side_effect=git_run) as run, \
            pytest.raises(RuntimeError, match='Git conflict'):
        worker.checkpoint(tmp_path)
    publish.assert_not_called()
    assert run.call_count == 1


def test_due_quote_precedes_slow_settlement_backfill(setup):
    root, cfg, fixture = setup
    clock = FakeClock('2026-10-10T10:45:00Z')
    run, calls = capture_stub(root, cfg, fixture)
    def slow_daily(path, command, *, clock):
        if command == 'daily':
            clock.now += timedelta(minutes=30)
        run(path, command, clock=clock)
    worker.watch(root, clock=clock, sleeper=clock.sleep, run=slow_daily, persist=Mock(), dispatch=Mock())
    assert calls[0][0] == 'capture'
    rows = load_observations(root)
    assert len(rows) == 1 and rows[0]['observed_at'] == '2026-10-10T10:45:00Z'
