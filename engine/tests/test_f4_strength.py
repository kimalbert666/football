"""Synthetic history checks; no real forecasts or old observations are changed."""
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import f4_strength as strength


NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


@pytest.fixture
def root(tmp_path):
    directory = tmp_path / 'data/01-teams'
    directory.mkdir(parents=True)
    (directory / '_aliases.json').write_text(json.dumps({'england': {
        'arsenal': {'espn': 'Arsenal'}, 'chelsea': {'espn': 'Chelsea'},
        'liverpool': {'espn': 'Liverpool'}, 'leeds': {'espn': 'Leeds United'}}}), encoding='utf-8')
    return tmp_path


def event(day, eid, *, home='Arsenal', away='Chelsea', score=(2, 1), status='STATUS_FULL_TIME'):
    date = f'{day[:4]}-{day[4:6]}-{day[6:]}T16:00:00Z'
    completed = status == 'STATUS_FULL_TIME'
    return {'id': eid, 'date': date, 'competitions': [{
        'id': eid, 'date': date, 'timeValid': True,
        'status': {'period': 2, 'type': {'name': status, 'completed': completed,
                                        'state': 'post' if completed else 'pre'}},
        'competitors': [
            {'id': '10', 'homeAway': 'home', 'team': {'id': '10', 'displayName': home}, 'score': str(score[0])},
            {'id': '20', 'homeAway': 'away', 'team': {'id': '20', 'displayName': away}, 'score': str(score[1])}]}]}


def transport(*, fail=(), mutate=None, games=5, extra_events=None):
    days = [f'2026100{x + 1}' for x in range(games)] + ['20261012']
    calls = []
    def fetch(url):
        calls.append(url)
        day = parse_qs(urlparse(url).query)['dates'][0]
        if day in fail:
            raise OSError('test-only provider secret should never persist')
        payload = {'leagues': [{'slug': 'eng.1', 'season': {
            'year': 2026, 'startDate': '2026-06-01T04:00Z', 'endDate': '2027-06-01T03:59Z',
            'displayName': 'synthetic season'},
            'calendar': [f'{d[:4]}-{d[4:6]}-{d[6:]}T07:00Z' for d in days]}],
            'events': [event(day, str(100 + days.index(day)))] if day != '20261012' else []}
        if extra_events and day in extra_events:
            payload['events'].extend(extra_events[day])
        if mutate:
            mutate(day, payload)
        return copy.deepcopy(payload)
    fetch.calls = calls
    return fetch


def fixture():
    return {'match_id': 'espn:999', 'provider': 'espn', 'league': 'england-premier',
            'home_id': 'arsenal', 'away_id': 'chelsea', 'kickoff_at': '2026-10-12T19:00:00Z'}


def refresh(root, fetch=None, **kwargs):
    return strength.refresh_strength(root, [fixture()], NOW, fetch=fetch or transport(),
                                     clock=lambda: NOW, **kwargs)


def test_complete_bootstrap_counts_real_games_and_uses_retrieval_time(root):
    state = refresh(root)
    assert state['status'] == 'ready'
    assert state['result_count'] == 5
    assert state['teams_with_five_games'] == 2
    assert state['rated_requested_fixtures'] == 1
    assert state['observed_at'] == '2026-10-12T12:00:00Z'
    assert state['available_for_historical_decisions'] is False
    snapshot = json.loads((root / state['snapshot_path']).read_text())
    assert len(snapshot['source_records']) == 6
    assert all(x['source']['captured_at'] == state['observed_at'] for x in snapshot['source_records'])
    ratings = strength.ratings_for_fixture(root, fixture(), NOW + timedelta(seconds=1))
    assert ratings['prior_games'] == [5, 5]
    assert ratings['home'] > ratings['away']
    assert ratings['source'] == strength.METHOD
    assert ratings['latest_result_kickoff_at'] == '2026-10-05T16:00:00Z'
    assert ratings['observed_at'] == state['observed_at']


def test_old_decision_newer_results_never_retroactively_receive_ratings(root):
    refresh(root)
    old = {**fixture(), 'kickoff_at': '2026-10-10T19:00:00Z'}
    assert strength.ratings_for_fixture(root, old, NOW - timedelta(days=2)) is None
    assert strength.ratings_for_fixture(root, fixture(), NOW - timedelta(microseconds=1)) is None
    assert strength.ratings_for_fixture(root, fixture(), NOW + timedelta(days=2)) is None


def test_no_fake_minimum_and_swapped_venue_reads_actual_team_elo(root):
    refresh(root, transport(games=4))
    assert strength.ratings_for_fixture(root, fixture(), NOW + timedelta(seconds=1)) is None
    refresh(root, force=True)
    normal = strength.ratings_for_fixture(root, fixture(), NOW + timedelta(seconds=1))
    reverse = strength.ratings_for_fixture(root, {**fixture(), 'home_id': 'chelsea', 'away_id': 'arsenal'},
                                          NOW + timedelta(seconds=1))
    assert normal['home'] == reverse['away']
    assert normal['away'] == reverse['home']
    assert reverse['prior_games'] == [5, 5]


@pytest.mark.parametrize('changes', [
    {'provider': 'sporttery'}, {'league': 'championship'}, {'home_id': 'missing'},
    {'away_id': 'arsenal'}, {'kickoff_at': '2026-10-12T10:00:00Z'},
    {'kickoff_at': '2027-08-01T19:00:00Z'}, {'home_id': None},
])
def test_fixture_identity_deadline_and_season_are_checked(root, changes):
    refresh(root)
    assert strength.ratings_for_fixture(root, {**fixture(), **changes}, NOW + timedelta(seconds=1)) is None


def test_cache_only_rating_read_and_daily_refresh_throttle(root):
    fetch = transport()
    refresh(root, fetch)
    calls = len(fetch.calls)
    assert refresh(root, fetch)['refresh'] == 'cached_same_utc_day'
    assert len(fetch.calls) == calls
    assert strength.ratings_for_fixture(root, fixture(), NOW + timedelta(minutes=2))
    assert len(fetch.calls) == calls


def test_same_day_result_finishing_after_morning_snapshot_refreshes(root):
    before = transport(extra_events={'20261012': [event('20261012', '612', status='STATUS_SCHEDULED')]})
    morning = refresh(root, before)
    assert morning['result_count'] == 5
    snapshot = json.loads((root / morning['snapshot_path']).read_text())
    assert snapshot['source_records'][-1]['pending_results'][0]['match_id'] == 'espn:612'
    later = NOW + timedelta(hours=8)
    after = transport(extra_events={'20261012': [event('20261012', '612')]})
    evening = strength.refresh_strength(root, now=later, fetch=after, clock=lambda: later)
    assert evening['status'] == 'ready'
    assert evening['result_count'] == 6
    assert evening['snapshot_sha256'] != morning['snapshot_sha256']
    assert (root / morning['snapshot_path']).read_text() == json.dumps(snapshot, ensure_ascii=False,
        sort_keys=True, allow_nan=False, indent=2) + '\n'
    # A result retrieved that evening remains unavailable to the morning decision.
    original = strength.ratings_for_fixture(root, fixture(), NOW + timedelta(seconds=1))
    assert original['result_count'] == 5
    assert original['snapshot_sha256'] == morning['snapshot_sha256']


def test_same_utc_day_future_snapshot_does_not_skip_source_refresh(root):
    refresh(root)
    earlier = NOW - timedelta(hours=1)
    fetch = transport()
    state = strength.refresh_strength(root, now=earlier, fetch=fetch, clock=lambda: earlier)
    assert fetch.calls
    assert state['observed_at'] == '2026-10-12T11:00:00Z'
    assert state.get('refresh') is None


def test_completed_calendar_fixture_missing_from_cached_snapshot_forces_refresh(root):
    morning = refresh(root)
    later = NOW + timedelta(hours=8)
    fixture_completed = {**fixture(), 'match_id': 'espn:612', 'status': 'completed'}
    fetch = transport(extra_events={'20261012': [event('20261012', '612')]})
    state = strength.refresh_strength(root, [fixture_completed], later, fetch=fetch, clock=lambda: later)
    assert fetch.calls
    assert state['result_count'] == morning['result_count'] + 1


def test_same_day_cache_uses_utc_instead_of_local_date_and_rejects_corruption(root):
    state = refresh(root)
    local = NOW.astimezone(timezone(timedelta(hours=8)))
    fetch = transport()
    assert strength.refresh_strength(root, now=local, fetch=fetch, clock=lambda: local)['refresh'] == 'cached_same_utc_day'
    assert not fetch.calls
    path = root / state['snapshot_path']
    damaged = json.loads(path.read_text())
    damaged['result_count'] = 99
    path.write_text(json.dumps(damaged))
    repaired = strength.refresh_strength(root, now=local, fetch=fetch, clock=lambda: local)
    assert fetch.calls
    assert repaired['status'] == 'unavailable'
    assert json.loads(path.read_text()) == damaged  # Immutable corruption is reported, never overwritten.
    assert strength.ratings_for_fixture(root, fixture(), NOW + timedelta(minutes=2)) is None


def test_partial_source_failure_saves_progress_but_never_publishes_ready(root):
    state = refresh(root, transport(fail={'20261003'}))
    assert state['status'] == 'incomplete'
    assert state['missing_dates'] == ['20261003']
    assert state['verified_dates'] == 5
    assert list((root / 'data/f4/strength/calendar/2026').glob('*.json'))
    assert not (root / 'data/f4/strength/snapshots').exists()
    assert 'provider secret' not in json.dumps(state)
    repaired = transport()
    assert refresh(root, repaired)['status'] == 'ready'
    queried = [parse_qs(urlparse(x).query)['dates'][0] for x in repaired.calls]
    assert '20261001' not in queried
    assert '20261003' in queried


def test_failed_latest_refresh_keeps_previous_genuinely_observed_snapshot(root):
    ready = refresh(root)
    failed = refresh(root, transport(fail={'20261012'}), force=True)
    assert failed['status'] == 'unavailable'
    assert strength.ratings_for_fixture(root, fixture(), NOW + timedelta(minutes=2))['snapshot_sha256'] == ready['snapshot_sha256']


@pytest.mark.parametrize('kind', ['alias', 'score', 'kickoff', 'duplicate', 'future', 'season', 'league'])
def test_malformed_or_future_history_cannot_publish_snapshot(root, kind):
    def mutate(day, payload):
        if day != '20261001':
            return
        row = payload['events'][0]
        comp = row['competitions'][0]
        if kind == 'alias':
            comp['competitors'][0]['team']['displayName'] = 'Unresolved FC'
        elif kind == 'score':
            comp['competitors'][0]['score'] = '-1'
        elif kind == 'kickoff':
            comp['date'] = '2026-10-02T16:00Z'
        elif kind == 'duplicate':
            payload['events'].append(copy.deepcopy(row))
        elif kind == 'future':
            row['date'] = comp['date'] = '2026-10-13T16:00Z'
        elif kind == 'season':
            payload['leagues'][0]['season']['year'] = 2025
        else:
            payload['leagues'][0]['slug'] = 'eng.2'
    state = refresh(root, transport(mutate=mutate))
    assert state['status'] == 'incomplete'
    assert state['missing_dates'] == ['20261001']
    assert strength.ratings_for_fixture(root, fixture(), NOW + timedelta(minutes=2)) is None


def test_live_scheduled_and_postponed_matches_do_not_update_elo(root):
    added = {'20261005': [event('20261005', '501', status='STATUS_SCHEDULED'),
                         event('20261005', '502', status='STATUS_POSTPONED')]}
    assert refresh(root, transport(extra_events=added))['result_count'] == 5


def test_snapshot_tampering_and_future_source_timestamp_rejected(root):
    state = refresh(root)
    path = root / state['snapshot_path']
    original = json.loads(path.read_text())
    altered = copy.deepcopy(original)
    altered['ratings']['arsenal']['elo'] += 50
    path.write_text(json.dumps(altered))
    assert strength.ratings_for_fixture(root, fixture(), NOW + timedelta(seconds=1)) is None
    # Even a correctly rehashed snapshot cannot move its source capture into the future.
    altered = copy.deepcopy(original)
    source = altered['source_records'][0]
    source['source']['captured_at'] = '2026-10-13T12:00:00Z'
    source['record_sha256'] = strength._digest({k: v for k, v in source.items() if k != 'record_sha256'})
    altered['snapshot_sha256'] = strength._digest({k: v for k, v in altered.items() if k != 'snapshot_sha256'})
    (path.parent / (altered['snapshot_sha256'] + '.json')).write_text(json.dumps(altered))
    assert strength.ratings_for_fixture(root, fixture(), NOW + timedelta(seconds=1)) is None


def test_no_naive_time_or_invalid_age_limit(root):
    with pytest.raises(ValueError):
        strength.refresh_strength(root, now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        strength.ratings_for_fixture(root, fixture(), NOW.replace(tzinfo=None))
    for value in (True, 0, float('nan')):
        with pytest.raises(ValueError):
            strength.ratings_for_fixture(root, fixture(), NOW, max_age_hours=value)
