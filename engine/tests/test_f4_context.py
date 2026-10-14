"""Context joins must preserve actual availability times and unknown states."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

import f4_context as context
from f4_sources import _stamp


NOW = datetime(2026, 10, 12, 11, 0, tzinfo=timezone.utc)


@pytest.fixture
def root(tmp_path):
    path = tmp_path / 'data/01-teams/_aliases.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'england': {
        'arsenal': {'espn': 'Arsenal', 'understat': 'Arsenal'},
        'chelsea': {'espn': 'Chelsea', 'understat': 'Chelsea'},
    }}), encoding='utf-8')
    return tmp_path


def fixture(home='arsenal', away='chelsea'):
    return {'match_id': 'espn:101', 'provider': 'espn', 'league': 'england-premier',
            'home_id': home, 'away_id': away, 'home': 'Arsenal', 'away': 'Chelsea',
            'kickoff_at': _stamp(NOW + timedelta(hours=8)), 'status': 'scheduled'}


def fpl(year=2026, *, empty=False):
    return {'events': [{'id': 1, 'deadline_time': f'{year}-08-21T17:30:00Z'}],
            'teams': [{'id': 1, 'name': 'Arsenal'}, {'id': 2, 'name': 'Chelsea'}],
            'elements': [] if empty else [
                {'id': 1, 'team': 1, 'status': 'i', 'news': 'Detailed narrative never saved',
                 'news_added': '2026-10-10T11:00:00Z', 'chance_of_playing_next_round': 0},
                {'id': 2, 'team': 2, 'status': 'a'}]}


def match(mid, day, *, result=True, h=1.5, a=.8, home='Arsenal', away='Chelsea'):
    return {'id': str(mid), 'datetime': day + ' 19:00:00', 'isResult': result,
            'h': {'title': home}, 'a': {'title': away}, 'xG': {'h': h, 'a': a},
            'goals': {'h': '9', 'a': '8'}, 'forecast': {'w': .9}}


def transport(*, fpl_payload=None, xg_payload=None, schedules=None):
    calls = []
    def fetch(url):
        calls.append(url)
        if 'bootstrap-static' in url:
            return copy.deepcopy(fpl_payload if fpl_payload is not None else fpl())
        if 'getLeagueData' in url:
            return copy.deepcopy(xg_payload if xg_payload is not None else {'dates': [match(1, '2026-10-10')]})
        return copy.deepcopy((schedules or {}).get(url.split('/teams/')[1].split('/')[0]))
    fetch.calls = calls
    return fetch


def strength(root, *, year=2026, observed=NOW - timedelta(minutes=2)):
    row = {'schema': 'f4-strength-observed-v1', 'observed_at': _stamp(observed),
           'season': {'year': year, 'start_at': f'{year}-06-01T04:00:00Z',
                      'end_at': f'{year+1}-06-01T03:59:00Z'},
           'team_provider_ids': {'arsenal': '359', 'chelsea': '363'}, 'source_records': []}
    row['snapshot_sha256'] = context._digest(row)
    relative = 'data/f4/strength/snapshots/' + row['snapshot_sha256'] + '.json'
    context._write(root / relative, row)
    context._write(root / 'data/f4/strength/latest.json', {
        'status': 'ready', 'snapshot_path': relative, 'snapshot_sha256': row['snapshot_sha256']})


def schedule(team, native):
    def event(mid, day, slug='eng.1', completed=True):
        return {'id': str(mid), 'date': day, 'league': {'slug': slug}, 'season': {'year': 2026},
                'competitions': [{'date': day, 'status': {'type': {'completed': completed, 'state': 'post' if completed else 'pre'}},
                                  'competitors': [{'team': {'id': native}}, {'team': {'id': '999'}}]}]}
    return {'team': {'id': native, 'displayName': team}, 'requestedSeason': {'year': 2026},
            'events': [event(11, '2026-10-08T19:00:00Z', 'eng.league_cup'),
                       event(12, '2026-10-10T19:00:00Z', 'club.friendly'),
                       event(13, '2026-10-15T19:00:00Z'),
                       event(14, '2026-10-11T19:00:00Z', completed=False)]}


def load_snapshot(root, status):
    return json.loads((root / status['snapshot_path']).read_text(encoding='utf-8'))


def test_actual_read_time_not_backdated_by_query_now_or_transport(root):
    inner = transport()
    def fetch(url):
        return {'payload': inner(url), 'captured_at': '2020-01-01T00:00:00Z'}
    status = context.refresh_context(root, [fixture()], now=NOW - timedelta(days=3),
                                     clock=lambda: NOW, fetch=fetch)
    assert status['observed_at'] == _stamp(NOW)
    assert context.context_for_fixture(root, fixture(), NOW - timedelta(seconds=1)) is None
    assert context.context_for_fixture(root, fixture(), NOW + timedelta(minutes=1)) is not None


def test_xg_uses_only_completed_prior_dates_and_last_five(root):
    rows = [match(n, f'2026-10-{n:02d}', h=n) for n in range(1, 8)]
    rows += [match(100, '2026-10-12'), match(101, '2026-10-15'), match(102, '2026-10-10', result=False)]
    status = context.refresh_context(root, [fixture()], clock=lambda: NOW,
                                     fetch=transport(xg_payload={'dates': rows}))
    team = load_snapshot(root, status)['teams']['arsenal']['xg_history']
    assert team['prior_match_count'] == 7
    assert [x['source_match_id'] for x in team['last_five']] == ['3', '4', '5', '6', '7']
    assert team['mean_xg_for_last_five'] == 5
    encoded = json.dumps(team)
    assert 'goals' not in encoded and 'forecast' not in encoded


def test_flags_no_flags_and_empty_are_distinct_not_confirmed_absences(root):
    status = context.refresh_context(root, [fixture()], clock=lambda: NOW, fetch=transport())
    teams = load_snapshot(root, status)['teams']
    assert teams['arsenal']['availability']['status'] == 'reported_flags'
    assert teams['chelsea']['availability']['status'] == 'reported_no_flags'
    assert teams['arsenal']['availability']['confirmed_absences'] is None
    assert 'Detailed narrative' not in json.dumps(teams)
    later = NOW + timedelta(minutes=1)
    status = context.refresh_context(root, [fixture()], clock=lambda: later,
                                     fetch=transport(fpl_payload=fpl(empty=True)), force=True)
    assert load_snapshot(root, status)['teams']['arsenal']['availability']['status'] == 'unknown_source_empty'


def test_missing_fpl_and_no_strength_does_not_guess_current_season(root):
    def fetch(url):
        raise OSError('offline')
    status = context.refresh_context(root, [fixture()], clock=lambda: NOW, fetch=fetch)
    assert status['season_year'] is None
    assert status['source_statuses']['understat'] == 'not_requested'
    assert context.context_for_fixture(root, fixture(), NOW + timedelta(minutes=1)) is None


def test_stale_fpl_cannot_supply_fallback_season_or_later_fixture(root):
    status = context.refresh_context(root, [fixture()], clock=lambda: NOW,
                                     fetch=transport(fpl_payload=fpl(year=2025)))
    assert status['season_year'] is None
    assert status['source_statuses']['fpl'] == 'unavailable'
    assert status['source_statuses']['understat'] == 'not_requested'
    assert context.context_for_fixture(root, fixture(), NOW + timedelta(minutes=1)) is None


def test_fpl_fallback_bounds_reject_next_season_without_fixture_year(root):
    status = context.refresh_context(root, [fixture()], clock=lambda: NOW, fetch=transport())
    assert load_snapshot(root, status)['season']['end_at'] == '2027-05-31T23:59:59.999999Z'
    future = {**fixture(), 'kickoff_at': '2027-08-21T19:00:00Z'}
    assert context.context_for_fixture(root, future, NOW + timedelta(minutes=1)) is None


def test_old_season_xg_is_rejected_even_with_current_rows(root):
    payload = {'dates': [match(1, '2026-10-10'), match(2, '2025-10-10')]}
    status = context.refresh_context(root, [fixture()], clock=lambda: NOW,
                                     fetch=transport(xg_payload=payload))
    assert status['teams_xg'] == 0
    assert status['source_statuses']['understat'] == 'unavailable'
    assert load_snapshot(root, status)['teams']['arsenal']['xg_history']['status'] == 'unknown_no_prior_xg'


def test_verified_fpl_name_and_code_aliases_require_both_labels(root):
    aliases = {'manchester united': {'man-united'}, 'tottenham hotspur': {'tottenham'}}
    payload = {'teams': [{'id': 16, 'name': 'Man Utd', 'short_name': 'MUN'},
                         {'id': 19, 'name': 'Spurs', 'short_name': 'TOT'}],
               'elements': [{'id': 101, 'team': 16, 'status': 'a'},
                            {'id': 102, 'team': 19, 'status': 'd'}]}
    teams = context._availability(payload, aliases, NOW)
    assert teams['man-united']['status'] == 'reported_no_flags'
    assert teams['tottenham']['status'] == 'reported_flags'
    payload['teams'][0]['short_name'] = 'TOT'
    assert 'man-united' not in context._availability(payload, aliases, NOW)
    assert context._availability(payload, {}, NOW) == {}


def test_verified_espn_season_preferred_and_fpl_mismatch_skipped(root):
    strength(root)
    status = context.refresh_context(root, [], clock=lambda: NOW,
                                     fetch=transport(fpl_payload=fpl(year=2025)))
    assert status['season_year'] == 2026
    assert status['source_statuses']['fpl'] == 'unavailable'
    assert status['teams_availability'] == 0
    assert load_snapshot(root, status)['season']['source'] == 'verified-espn-strength-snapshot'


def test_club_gap_includes_cups_excludes_friendly_future_and_unfinished(root):
    strength(root)
    fetch = transport(schedules={'359': schedule('Arsenal', '359'), '363': schedule('Chelsea', '363')})
    status = context.refresh_context(root, [fixture()], clock=lambda: NOW, fetch=fetch)
    assert status['teams_club_schedule'] == 2
    value = context.context_for_fixture(root, fixture(), NOW + timedelta(minutes=1))
    club = value['home']['club_schedule']
    assert club['previous_formal_kickoff_at'] == '2026-10-08T19:00:00Z'
    assert club['kickoff_gap_hours'] == 96
    assert club['interpretation'] == 'club_kickoff_gap_not_confirmed_player_rest'
    assert len(club['last_formal_games']) == 1


def test_unresolved_team_is_unknown_never_neutral_or_healthy(root):
    status = context.refresh_context(root, [fixture(home='unmapped')], clock=lambda: NOW, fetch=transport())
    result = context.context_for_fixture(root, fixture(home='unmapped'), NOW + timedelta(minutes=1))
    assert result['home']['availability']['status'] == 'unknown_unresolved_team'
    assert result['home']['club_schedule']['kickoff_gap_hours'] is None
    assert 'unmapped' in status['unresolved_clubs']


def test_cache_only_strict_age_and_fixture_identity(root, monkeypatch):
    context.refresh_context(root, [fixture()], clock=lambda: NOW, fetch=transport())
    monkeypatch.setattr(context, '_fetch', lambda _: pytest.fail('cache read must not fetch'))
    assert context.context_for_fixture(root, fixture(), NOW + timedelta(hours=1))
    assert context.context_for_fixture(root, fixture(), NOW + timedelta(hours=2), max_age_hours=1) is None
    assert context.context_for_fixture(root, fixture(away='arsenal'), NOW + timedelta(minutes=1)) is None
    other = {**fixture(), 'season_year': 2025}
    assert context.context_for_fixture(root, other, NOW + timedelta(minutes=1)) is None


def test_snapshot_tampering_rejected(root):
    status = context.refresh_context(root, [fixture()], clock=lambda: NOW, fetch=transport())
    path = root / status['snapshot_path']
    row = load_snapshot(root, status)
    row['teams']['arsenal']['xg_history']['mean_xg_for_last_five'] = 100
    context._write(path, row)
    assert context.context_for_fixture(root, fixture(), NOW + timedelta(minutes=1)) is None


def test_daily_cache_no_repeat_network_and_immutable_history(root):
    fetch = transport()
    one = context.refresh_context(root, [fixture()], clock=lambda: NOW, fetch=fetch)
    first_path = root / one['snapshot_path']
    first = first_path.read_bytes()
    two = context.refresh_context(root, [fixture()], clock=lambda: NOW + timedelta(hours=1), fetch=fetch)
    assert two['refresh'] == 'cached_same_utc_day'
    assert len(fetch.calls) == 2
    context.refresh_context(root, [fixture()], clock=lambda: NOW + timedelta(minutes=2), fetch=fetch, force=True)
    assert first_path.read_bytes() == first
    assert len(list(first_path.parent.glob('*.json'))) == 2


@pytest.mark.parametrize('bad_xg', [float('nan'), float('inf'), -1, True])
def test_invalid_xg_does_not_enter_cache(root, bad_xg):
    status = context.refresh_context(root, [fixture()], clock=lambda: NOW,
                                     fetch=transport(xg_payload={'dates': [match(1, '2026-10-10', h=bad_xg)]}))
    assert status['teams_xg'] == 0
    assert status['source_statuses']['understat'] == 'unavailable'


def test_future_injury_timestamp_is_unverified(root):
    payload = fpl()
    payload['elements'][0]['news_added'] = '2026-10-15T11:00:00Z'
    status = context.refresh_context(root, [], clock=lambda: NOW,
                                     fetch=transport(fpl_payload=payload))
    assert status['teams_availability'] == 0
    assert status['source_statuses']['fpl'] == 'unavailable'


def test_rest_identity_conflict_is_not_saved_as_available(root):
    strength(root)
    wrong = schedule('Chelsea', '363')
    status = context.refresh_context(root, [fixture()], clock=lambda: NOW,
                                     fetch=transport(schedules={'359': wrong, '363': wrong}))
    assert status['teams_club_schedule'] == 1
    assert load_snapshot(root, status)['teams']['arsenal']['club_schedule']['status'] == 'unavailable'
