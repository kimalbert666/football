"""Observed-before-decision, optional football context; no betting or model fitting.

Daily/background refresh only. Quote-window reads are cache-only. Today-retrieved
history is never represented as context available to an earlier decision. FPL
statuses are reported availability flags, not confirmed absences; club kickoff
gaps do not measure an individual player's international duty or actual rest.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from f4_sources import _aliases, _instant, _resolve, _stamp

SCHEMA = 'f4-prematch-context-observed-v1'
FPL_URL = 'https://fantasy.premierleague.com/api/bootstrap-static/'
UNDERSTAT_URL = 'https://understat.com/getLeagueData/EPL/{year}'
ESPN_ALL = 'https://site.api.espn.com/apis/site/v2/sports/soccer/all/'
FORMAL_COMPETITIONS = {'eng.1', 'eng.fa', 'eng.league_cup', 'uefa.champions',
                       'uefa.europa', 'uefa.europa.conf', 'fifa.club', 'uefa.super_cup'}
MAX_TEAMS = 20
# These are exact FPL name/code pairs observed on the official bootstrap feed.
# Expand through the existing canonical registry; never infer by a fuzzy name
# or by a provider ID that can be reused between seasons.
FPL_LABELS = {('Man Utd', 'MUN'): 'Manchester United',
              ('Spurs', 'TOT'): 'Tottenham Hotspur'}


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         allow_nan=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True,
                               allow_nan=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def _utc(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('timezone-aware clock required')
    return value.astimezone(timezone.utc)


def _fetch(url):
    import requests
    headers = {'X-Requested-With': 'XMLHttpRequest'} if url.startswith('https://understat.com/') else {}
    response = requests.get(url, headers=headers, timeout=20, allow_redirects=False)
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError('source redirected or non-200')
    return {'payload': response.json(), 'headers': dict(response.headers)}


def _retrieve(url, fetch, clock):
    received = fetch(url)
    # The local read clock is authoritative: an injected / source timestamp is
    # evidence about its feed, never permission to backdate this observation.
    observed = _utc(clock())
    payload = received.get('payload') if isinstance(received, dict) and 'payload' in received else received
    if not isinstance(payload, (dict, list)):
        raise ValueError('source JSON missing')
    return payload, {'url': url, 'observed_at': _stamp(observed),
                     'content_sha256': _digest(payload)}, observed


def _strength_metadata(root, observed):
    latest = _read(root / 'data/f4/strength/latest.json')
    if not latest or latest.get('status') != 'ready':
        return None, {}
    relative = latest.get('snapshot_path')
    if not isinstance(relative, str):
        return None, {}
    path = (root / relative).resolve()
    expected = (root / 'data/f4/strength/snapshots').resolve()
    if expected not in path.parents:
        return None, {}
    row = _read(path)
    if not row:
        return None, {}
    sha = row.get('snapshot_sha256')
    content = {k: v for k, v in row.items() if k != 'snapshot_sha256'}
    try:
        season = row['season']
        if (sha != latest.get('snapshot_sha256') or sha != _digest(content)
                or _instant(row['observed_at']) > observed
                or type(season['year']) is not int
                or not _instant(season['start_at']) <= observed <= _instant(season['end_at'])):
            return None, {}
        ids = row.get('team_provider_ids') or {}
        # Compatibility with snapshots that preserve IDs on verified results.
        if not ids:
            ids = {}
            for source in row.get('source_records', []):
                for event in source.get('results', []):
                    for side in ('home', 'away'):
                        canonical, native = event.get(side + '_id'), event.get(side + '_provider_id')
                        if canonical and native is not None:
                            if canonical in ids and str(ids[canonical]) != str(native):
                                return None, {}
                            ids[canonical] = str(native)
        return {**season, 'source': 'verified-espn-strength-snapshot',
                'metadata_observed_at': row['observed_at']}, {
                    k: str(v) for k, v in ids.items() if isinstance(k, str) and str(v).isdigit()}
    except (KeyError, TypeError, ValueError):
        return None, {}


def _fpl_season(payload, observed):
    events = payload.get('events') if isinstance(payload, dict) else None
    choices = [e for e in events or [] if isinstance(e, dict) and e.get('id') == 1]
    if len(choices) != 1:
        raise ValueError('FPL current season metadata unresolved')
    start = _instant(choices[0]['deadline_time'])
    if start > observed:
        raise ValueError('FPL first deadline is still in the future')
    lower = datetime(start.year, 6, 1, tzinfo=timezone.utc)
    upper = datetime(start.year + 1, 6, 1, tzinfo=timezone.utc)
    if not lower <= observed < upper:
        raise ValueError('FPL first deadline belongs to a stale season')
    return {'year': start.year, 'start_at': _stamp(lower),
            'end_at': _stamp(upper - timedelta(microseconds=1)),
            'first_deadline_at': _stamp(start), 'source': 'fpl-gw1-deadline',
            'bounds_policy': 'gw1_year_june_to_june_utc',
            'metadata_observed_at': _stamp(observed)}


def _availability(payload, aliases, observed):
    if not isinstance(payload, dict) or not isinstance(payload.get('teams'), list) or not isinstance(payload.get('elements'), list):
        raise ValueError('FPL team/player lists missing')
    native = {}
    result = {}
    for team in payload['teams']:
        label = FPL_LABELS.get((team.get('name'), team.get('short_name')))
        canonical = _resolve([team.get('name'), team.get('short_name'), label], aliases)
        if canonical and type(team.get('id')) is int:
            if canonical in result or team['id'] in native:
                raise ValueError('FPL team identity duplicated')
            native[team['id']] = canonical
            result[canonical] = {'status': 'unknown_source_empty', 'observed_at': _stamp(observed),
                                 'source': 'fpl-reported', 'roster_count': 0, 'flag_counts': {},
                                 'flagged_players': [], 'confirmed_absences': None,
                                 'interpretation': 'availability_flags_are_not_confirmed_absences'}
    seen = set()
    for player in payload['elements']:
        canonical = native.get(player.get('team'))
        if not canonical:
            continue
        pid = player.get('id')
        if type(pid) is not int or pid in seen:
            raise ValueError('FPL player identity missing or duplicated')
        seen.add(pid)
        team = result[canonical]
        team['roster_count'] += 1
        flag = player.get('status')
        if flag not in {'a', 'd', 'i', 's', 'u', 'n'}:
            raise ValueError('FPL availability flag unknown')
        team['flag_counts'][flag] = team['flag_counts'].get(flag, 0) + 1
        if flag == 'a':
            continue
        news_added = player.get('news_added')
        if news_added:
            if _instant(news_added) > observed:
                raise ValueError('FPL availability update is in future')
            news_added = _stamp(_instant(news_added))
        selected = {'player_id': str(pid), 'status': flag, 'news_added': news_added,
                    'category': {'d': 'doubtful', 'i': 'injury_reported', 's': 'suspension_reported',
                                 'u': 'unavailable_not_necessarily_injured', 'n': 'not_available'}[flag]}
        for key in ('chance_of_playing_this_round', 'chance_of_playing_next_round'):
            value = player.get(key)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100):
                raise ValueError('FPL playing chance invalid')
            selected[key] = value
        # No full news text, price, season totals or fantasy points enter cache.
        team['flagged_players'].append(selected)
    for team in result.values():
        if team['roster_count']:
            team['status'] = 'reported_flags' if team['flagged_players'] else 'reported_no_flags'
    return result


def _number(value):
    if isinstance(value, bool):
        raise ValueError('boolean xG is invalid')
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError('xG must be finite nonnegative')
    return number


def _xg_history(payload, aliases, observed, season):
    if not isinstance(payload, dict) or not isinstance(payload.get('dates'), list):
        raise ValueError('Understat season match list missing')
    histories = {}
    seen = set()
    if (not isinstance(season, dict) or type(season.get('year')) is not int
            or not season.get('start_at') or not season.get('end_at')):
        raise ValueError('Understat requested season bounds unverified')
    season_start = _instant(season['start_at']).date()
    season_end = (_instant(season['end_at']) + timedelta(microseconds=1)).date()
    # Understat's datetime has no timezone. Excluding today's entire date avoids
    # inventing a UTC offset or admitting an ongoing / future same-day match.
    cutoff_day = observed.date()
    for match in payload['dates']:
        if match.get('isResult') is not True:
            continue
        source_date = datetime.fromisoformat(match['datetime']).date()
        if not season_start <= source_date < season_end:
            raise ValueError('Understat result belongs to a different requested season')
        if source_date >= cutoff_day:
            continue
        mid = str(match.get('id', ''))
        home = _resolve([match.get('h', {}).get('title')], aliases)
        away = _resolve([match.get('a', {}).get('title')], aliases)
        if not mid.isdigit() or mid in seen:
            raise ValueError('Understat match identity missing or duplicated')
        seen.add(mid)
        if not home or not away or home == away:
            continue
        prices = match.get('xG') or {}
        if prices.get('h') is None or prices.get('a') is None:
            continue
        values = [_number(prices[s]) for s in ('h', 'a')]
        for canonical, opponent, side, index in ((home, away, 'home', 0), (away, home, 'away', 1)):
            histories.setdefault(canonical, []).append({'source_match_id': mid, 'match_date': source_date.isoformat(),
                                                        'opponent_id': opponent, 'venue': side,
                                                        'xg_for': values[index], 'xg_against': values[1 - index]})
    result = {}
    for team, games in histories.items():
        games.sort(key=lambda g: (g['match_date'], g['source_match_id']))
        last = games[-5:]
        result[team] = {'status': 'available', 'observed_at': _stamp(observed), 'source': 'understat',
                        'prior_match_count': len(games), 'last_five': last,
                        'mean_xg_for_last_five': sum(x['xg_for'] for x in last) / len(last),
                        'mean_xg_against_last_five': sum(x['xg_against'] for x in last) / len(last),
                        'date_policy': 'completed_matches_strictly_before_observation_calendar_date',
                        'historical_publication_time_verified': False}
    return result


def _club_schedule(payload, canonical, native, year, aliases, observed):
    if (not isinstance(payload, dict) or str(payload.get('team', {}).get('id')) != native
            or _resolve([payload.get('team', {}).get(k) for k in ('displayName', 'name', 'shortDisplayName')], aliases) != canonical
            or str(payload.get('requestedSeason', {}).get('year')) != str(year)
            or not isinstance(payload.get('events'), list)):
        raise ValueError('ESPN club schedule identity or season unverified')
    games, seen = [], set()
    for event in payload['events']:
        competitions = event.get('competitions') or []
        if len(competitions) != 1:
            continue
        comp = competitions[0]
        state = comp.get('status', {}).get('type', {})
        if state.get('completed') is not True or state.get('state') != 'post':
            continue
        slug = event.get('league', {}).get('slug')
        if slug not in FORMAL_COMPETITIONS:
            continue
        kickoff = _instant(comp.get('date') or event.get('date'))
        if kickoff >= observed:
            continue
        if str(event.get('season', {}).get('year')) != str(year):
            continue
        mid = str(event.get('id', ''))
        teams = [c.get('team', {}) for c in comp.get('competitors', [])]
        if (not mid.isdigit() or mid in seen or len(teams) != 2
                or sum(str(t.get('id')) == native for t in teams) != 1):
            raise ValueError('ESPN completed club fixture identity ambiguous')
        seen.add(mid)
        games.append({'match_id': 'espn:' + mid, 'kickoff_at': _stamp(kickoff), 'competition': slug})
    games.sort(key=lambda g: g['kickoff_at'])
    return {'status': 'available' if games else 'unknown_no_completed_formal_games',
            'observed_at': _stamp(observed), 'source': 'espn-all-team-schedule',
            'provider_team_id': native, 'last_formal_games': games[-5:],
            'coverage': 'reported_formal_club_matches_only_not_individual_or_international_rest'}


def refresh_context(root, fixtures, now=None, clock=None, fetch=None, force=False):
    """Refresh background context once per UTC day; injected fetch(url) returns JSON.

    A transport may alternatively return {payload, headers, captured_at}. Its
    captured_at is intentionally not used to label the actual local observation.
    At most twenty future-fixture clubs are requested, with four workers and a
    default twenty-second deadline per HTTP request. No lineup / quote calls.
    """
    root = Path(root)
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = _utc(now if now is not None else clock())
    actual = _utc(clock())
    latest_path = root / 'data/f4/context/latest.json'
    previous = _read(latest_path)
    if not force and previous:
        try:
            if _instant(previous['observed_at']).date() == actual.date() and _instant(previous['observed_at']) <= actual:
                return {**previous, 'refresh': 'cached_same_utc_day'}
        except (KeyError, TypeError, ValueError):
            pass
    fetch = fetch or _fetch
    aliases = _aliases(root)
    season, native_ids = _strength_metadata(root, actual)
    sources, problems, availability, xg, rests = {}, [], {}, {}, {}
    try:
        payload, meta, fetched = _retrieve(FPL_URL, fetch, clock)
        fpl_season = _fpl_season(payload, fetched)
        if season and fpl_season['year'] != season['year']:
            raise ValueError('FPL season differs from verified ESPN season')
        season = season or fpl_season
        availability = _availability(payload, aliases, fetched)
        sources['fpl'] = {**meta, 'status': 'available' if any(v['roster_count'] for v in availability.values()) else 'empty',
                          'season_year': fpl_season['year'], 'historical_publication_time_verified': False}
    except Exception as exc:
        sources['fpl'] = {'url': FPL_URL, 'status': 'unavailable', 'error': type(exc).__name__}
        problems.append('FPL availability missing or unverified: ' + str(exc)[:120])
    if season:
        url = UNDERSTAT_URL.format(year=season['year'])
        try:
            payload, meta, fetched = _retrieve(url, fetch, clock)
            xg = _xg_history(payload, aliases, fetched, season)
            sources['understat'] = {**meta, 'status': 'available' if xg else 'empty', 'season_year': season['year']}
        except Exception as exc:
            sources['understat'] = {'url': url, 'status': 'unavailable', 'error': type(exc).__name__}
            problems.append('Understat lagged xG missing or unverified: ' + str(exc)[:120])
    else:
        sources['understat'] = {'status': 'not_requested', 'reason': 'current_season_unresolved'}
    wanted = set()
    for fixture in fixtures:
        try:
            if (fixture.get('provider') == 'espn' and fixture.get('league') == 'england-premier'
                    and fixture.get('status') == 'scheduled' and now < _instant(fixture['kickoff_at']) <= now + timedelta(days=7)):
                for side in ('home', 'away'):
                    if fixture.get(side + '_id'):
                        wanted.add(fixture[side + '_id'])
        except (KeyError, TypeError, ValueError):
            continue
    requested = sorted(wanted)[:MAX_TEAMS]

    def fetch_rest(team):
        native = native_ids.get(team)
        if not season or not native:
            return team, {'status': 'unknown_provider_identity', 'last_formal_games': []}, None
        url = ESPN_ALL + f'teams/{native}/schedule?' + urlencode({'season': season['year']})
        try:
            payload, meta, fetched = _retrieve(url, fetch, clock)
            return team, _club_schedule(payload, team, native, season['year'], aliases, fetched), {**meta, 'status': 'available'}
        except Exception as exc:
            return team, {'status': 'unavailable', 'last_formal_games': [], 'error': type(exc).__name__}, {'url': url, 'status': 'unavailable', 'error': type(exc).__name__}

    with ThreadPoolExecutor(max_workers=4) as pool:
        for team, value, meta in pool.map(fetch_rest, requested):
            rests[team] = value
            if meta:
                sources['espn-rest:' + team] = meta
    observed = _utc(clock())
    teams = {team: {'xg_history': xg.get(team, {'status': 'unknown_no_prior_xg'}),
                    'availability': availability.get(team, {'status': 'unknown_unresolved_team', 'confirmed_absences': None}),
                    'club_schedule': rests.get(team, {'status': 'not_requested', 'last_formal_games': []})}
             for team in sorted(set(xg) | set(availability) | set(rests))}
    snapshot = {'schema': SCHEMA, 'observed_at': _stamp(observed), 'season': season,
                'availability_policy': 'genuinely_retrieved_before_future_decision_only',
                'model_feature_status': 'context_only_not_used_in_current_M1_M2_M3',
                'sources': sources, 'teams': teams}
    snapshot['snapshot_sha256'] = _digest(snapshot)
    relative = 'data/f4/context/snapshots/' + snapshot['snapshot_sha256'] + '.json'
    path = root / relative
    if not path.exists():
        _write(path, snapshot)
    status = {'schema': SCHEMA, 'status': 'ready' if season and (xg or availability) else 'unavailable',
              'observed_at': snapshot['observed_at'], 'season_year': season['year'] if season else None,
              'snapshot_sha256': snapshot['snapshot_sha256'], 'snapshot_path': relative,
              'teams_xg': len(xg), 'teams_availability': sum(v['roster_count'] > 0 for v in availability.values()),
              'teams_club_schedule': sum(v['status'] == 'available' for v in rests.values()),
              'requested_clubs': len(requested), 'unresolved_clubs': sorted(wanted - set(native_ids)),
              'source_statuses': {k: v['status'] for k, v in sources.items()}, 'problems': problems,
              'model_feature_status': snapshot['model_feature_status']}
    _write(latest_path, status)
    return status


def context_for_fixture(root, fixture, decision, max_age_hours=48):
    """Read a verified, cached snapshot actually available before a future fixture."""
    root = Path(root)
    decision = _utc(decision)
    if (fixture.get('provider') != 'espn' or fixture.get('league') != 'england-premier'
            or not fixture.get('home_id') or not fixture.get('away_id') or fixture['home_id'] == fixture['away_id']):
        return None
    try:
        kickoff = _instant(fixture['kickoff_at'])
        if decision >= kickoff or max_age_hours < 0:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    candidates = []
    for path in (root / 'data/f4/context/snapshots').glob('*.json'):
        row = _read(path)
        if not row or row.get('schema') != SCHEMA:
            continue
        try:
            observed = _instant(row['observed_at'])
            season = row['season']
            if (observed > decision or (decision - observed).total_seconds() > max_age_hours * 3600
                    or not season or type(season.get('year')) is not int
                    or row.get('snapshot_sha256') != _digest({k: v for k, v in row.items() if k != 'snapshot_sha256'})
                    or path.stem != row['snapshot_sha256']):
                continue
            if season.get('end_at') and not _instant(season['start_at']) <= kickoff <= _instant(season['end_at']):
                continue
            if fixture.get('season_year') is not None and str(fixture['season_year']) != str(season['year']):
                continue
            for source in row.get('sources', {}).values():
                if source.get('observed_at') and _instant(source['observed_at']) > observed:
                    raise ValueError('source observation occurs after snapshot')
            candidates.append((observed, row))
        except (KeyError, TypeError, ValueError):
            continue
    if not candidates:
        return None
    _, row = max(candidates, key=lambda pair: pair[0])
    result = {'schema': SCHEMA, 'observed_at': row['observed_at'], 'snapshot_sha256': row['snapshot_sha256'],
              'season_year': row['season']['year'], 'model_feature_status': row['model_feature_status']}
    for side in ('home', 'away'):
        team = copy.deepcopy(row.get('teams', {}).get(fixture[side + '_id'], {}))
        if not team:
            team = {'xg_history': {'status': 'unknown_unresolved_team'},
                    'availability': {'status': 'unknown_unresolved_team', 'confirmed_absences': None},
                    'club_schedule': {'status': 'unknown_unresolved_team'}}
        schedule = team.get('club_schedule', {})
        prior = [x for x in schedule.get('last_formal_games', []) if _instant(x['kickoff_at']) < decision]
        if prior:
            previous = max(prior, key=lambda x: x['kickoff_at'])
            schedule['previous_formal_kickoff_at'] = previous['kickoff_at']
            schedule['kickoff_gap_hours'] = (kickoff - _instant(previous['kickoff_at'])).total_seconds() / 3600
            schedule['interpretation'] = 'club_kickoff_gap_not_confirmed_player_rest'
        else:
            schedule['kickoff_gap_hours'] = None
        result[side] = team
    return result
