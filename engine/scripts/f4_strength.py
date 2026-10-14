"""Price-independent, observed-before-decision EPL strength snapshots.

The initial bootstrap retrieves completed ESPN results for the season actually
declared by ESPN. Historical results retrieved today become available *today*;
this module never pretends they were observed before an old betting decision.
Refresh belongs after quote persistence / outside capture windows. Rating reads
are cache-only and retain the existing M2 five-known-games minimum.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from f4_sources import ESPN_BASE, _aliases, _espn_event, _instant, _stamp

SCHEMA = 'f4-strength-observed-v1'
METHOD = 'verified-espn-season-results-elo-k20-ha60-v1'
MAX_CALENDAR_DAYS = 150
MAX_EVENTS_PER_DAY = 1000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True,
                               allow_nan=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def _read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _fetch(url):
    import requests
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def _season(payload, now):
    choices = [row for row in payload.get('leagues', [])
               if isinstance(row, dict) and row.get('slug') == 'eng.1']
    if len(choices) != 1:
        raise ValueError('ESPN EPL metadata missing or ambiguous')
    league = choices[0]
    season = league.get('season')
    if not isinstance(season, dict) or type(season.get('year')) is not int:
        raise ValueError('ESPN season identity missing')
    start, end = _instant(season.get('startDate')), _instant(season.get('endDate'))
    if not start <= now <= end:
        raise ValueError('ESPN declared season does not contain query time')
    calendar = league.get('calendar')
    if not isinstance(calendar, list) or not calendar:
        raise ValueError('ESPN declared season calendar missing')
    days = set()
    for value in calendar:
        when = _instant(value)
        if not start.date() <= when.date() <= end.date():
            raise ValueError('ESPN calendar outside declared season')
        if when.date() <= now.date():
            days.add(when.strftime('%Y%m%d'))
    if len(days) > MAX_CALENDAR_DAYS:
        raise ValueError('ESPN calendar exceeds bounded bootstrap')
    return {'year': season['year'], 'start_at': _stamp(start), 'end_at': _stamp(end),
            'name': season.get('displayName'), 'days': sorted(days)}


def _date_record(payload, day, season, aliases, url, captured):
    if not isinstance(payload, dict):
        raise ValueError('ESPN source response must be an object')
    declared = _season(payload, captured)
    if declared['year'] != season['year'] or declared['start_at'] != season['start_at']:
        raise ValueError('ESPN history season identity changed')
    events = payload.get('events')
    if not isinstance(events, list) or len(events) >= MAX_EVENTS_PER_DAY:
        raise ValueError('ESPN date events missing or potentially truncated')
    records, pending_results, identities = [], [], set()
    for event in events:
        row = _espn_event(event, aliases)
        if row['problems'] or row['status'] == 'unknown':
            raise ValueError('ESPN history identity or regulation result unresolved')
        if _instant(row['kickoff_at']).strftime('%Y%m%d') != day:
            raise ValueError('ESPN history event outside requested UTC date')
        if row['match_id'] in identities:
            raise ValueError('ESPN history duplicate match identity')
        identities.add(row['match_id'])
        if row['status'] != 'completed':
            if row['status'] in ('scheduled', 'live'):
                pending_results.append({key: row[key] for key in
                                        ('match_id', 'kickoff_at', 'status')})
            continue
        if _instant(row['kickoff_at']) >= captured:
            raise ValueError('ESPN completed result is in the future')
        record = {key: row[key] for key in
                  ('match_id', 'home_id', 'away_id', 'home', 'away', 'kickoff_at', 'score')}
        for side in ('home', 'away'):
            competitor = next(c for c in event['competitions'][0]['competitors'] if c['homeAway'] == side)
            native = str(competitor['team'].get('id', ''))
            if not native.isdigit():
                raise ValueError('ESPN historical native team identity missing')
            record[side + '_provider_id'] = native
        records.append(record)
    value = {'schema': SCHEMA, 'day': day, 'season_year': season['year'],
             'source': {'name': 'espn', 'url': url, 'captured_at': _stamp(captured),
                        'content_sha256': _digest(payload)},
             'events_seen': len(events), 'results': records,
             'pending_results': pending_results}
    value['record_sha256'] = _digest(value)
    return value


def _cached_day(value, day, year, observed):
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        return False
    if value.get('day') != day or value.get('season_year') != year:
        return False
    try:
        original = {k: v for k, v in value.items() if k != 'record_sha256'}
        return value.get('record_sha256') == _digest(original) and (
            _instant(value['source']['captured_at']) <= observed)
    except (KeyError, TypeError, ValueError):
        return False


def _same_day_ready(root, state, fixtures, now):
    """Throttle only an intact, already-observed snapshot with no result due.

    A morning snapshot may precede a match completed later on the same UTC day.
    Retaining those pending identities avoids treating the date cache as final.
    Old snapshots without pending-result metadata are refreshed once.
    """
    try:
        observed = _instant(state['observed_at'])
        if observed > now or observed.date() != now.date():
            return False
        relative = Path(state['snapshot_path'])
        if relative.is_absolute() or '..' in relative.parts:
            return False
        snapshot = _read(Path(root) / relative)
        if not snapshot or snapshot.get('snapshot_sha256') != state['snapshot_sha256']:
            return False
        body = {k: v for k, v in snapshot.items() if k != 'snapshot_sha256'}
        if _digest(body) != state['snapshot_sha256'] or snapshot.get('observed_at') != state['observed_at']:
            return False
        results = set()
        for record in snapshot['source_records']:
            if not _cached_day(record, record['day'], snapshot['season']['year'], observed):
                return False
            results.update(row['match_id'] for row in record['results'])
            # Recent metadata must explicitly attest whether any games remain.
            if record['day'] >= (now.date() - timedelta(days=2)).strftime('%Y%m%d'):
                if not isinstance(record.get('pending_results'), list):
                    return False
                if any(_instant(row['kickoff_at']) <= now for row in record['pending_results']):
                    return False
        if any(row.get('status') == 'completed' and row.get('match_id') not in results
               for row in fixtures):
            return False
        return True
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _ratings(results, observed):
    ratings, counts = defaultdict(lambda: 1500.0), Counter()
    seen = set()
    for row in sorted(results, key=lambda r: (r['kickoff_at'], r['match_id'])):
        h, a, identity = row['home_id'], row['away_id'], row['match_id']
        if not isinstance(identity, str) or not identity.startswith('espn:') or identity in seen:
            raise ValueError('strength result identity duplicate or unsupported')
        if not isinstance(h, str) or not h or not isinstance(a, str) or not a or h == a:
            raise ValueError('strength team identity invalid')
        if _instant(row['kickoff_at']) >= observed:
            raise ValueError('strength result unavailable at snapshot')
        scores = row['score']
        if not isinstance(scores, list) or len(scores) != 2 or any(type(x) is not int or x < 0 for x in scores):
            raise ValueError('strength requires regulation scores')
        seen.add(identity)
        expected = 1 / (1 + 10 ** ((ratings[a] - ratings[h] - 60) / 400))
        actual = 1.0 if scores[0] > scores[1] else 0.5 if scores[0] == scores[1] else 0.0
        move = 20 * (actual - expected)
        ratings[h] += move
        ratings[a] -= move
        counts[h] += 1
        counts[a] += 1
    return {team: {'elo': ratings[team], 'prior_games': counts[team]}
            for team in sorted(counts)}


def refresh_strength(root, fixtures=(), now=None, *, fetch=None, clock=None, force=False):
    """Daily refresh; max four ESPN calls concurrently, cache progress on failure.

    ``now`` chooses the query day. ``clock`` defaults to the actual UTC clock and
    supplies availability timestamps only after each response has arrived.
    A usable snapshot is emitted only when every ESPN-declared past match date
    was fetched and verified. Older dates are reused; the last three UTC dates
    are refreshed for delayed finishes/corrections. Raw odds feeds are not saved.
    """
    root = Path(root)
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = now or clock()
    if now.utcoffset() is None:
        raise ValueError('strength query time requires timezone')
    now = now.astimezone(timezone.utc)
    latest_path = root / 'data/f4/strength/latest.json'
    previous = _read(latest_path)
    fixtures = list(fixtures)
    if not force and previous and previous.get('status') == 'ready' and _same_day_ready(root, previous, fixtures, now):
        return {**previous, 'refresh': 'cached_same_utc_day'}
    fetch = fetch or _fetch
    status = {'schema': SCHEMA, 'method': METHOD, 'status': 'unavailable',
              'coverage_scope': 'all_past_dates_in_espn_declared_current_season_calendar',
              'available_for_historical_decisions': False, 'problems': []}
    try:
        aliases = _aliases(root)
        url = ESPN_BASE + 'scoreboard?' + urlencode({'dates': now.strftime('%Y%m%d')})
        payload = fetch(url)
        captured = clock().astimezone(timezone.utc)
        season = _season(payload, now)
        status['season'] = season
        status['metadata_source'] = {'url': url, 'captured_at': _stamp(captured),
                                     'content_sha256': _digest(payload)}
        cache_dir = root / 'data/f4/strength/calendar' / str(season['year'])
        records, pending = {}, []
        for day in season['days']:
            cache = _read(cache_dir / (day + '.json'))
            recent = datetime.strptime(day, '%Y%m%d').date() >= now.date() - timedelta(days=2)
            if not recent and _cached_day(cache, day, season['year'], captured):
                records[day] = cache
            else:
                pending.append(day)

        def fetch_day(day):
            endpoint = ESPN_BASE + 'scoreboard?' + urlencode({'dates': day, 'limit': MAX_EVENTS_PER_DAY})
            try:
                content = fetch(endpoint)
                observed = clock().astimezone(timezone.utc)
                return day, _date_record(content, day, season, aliases, endpoint, observed), None
            except Exception as exc:
                return day, None, type(exc).__name__

        with ThreadPoolExecutor(max_workers=4) as pool:
            for day, value, error in pool.map(fetch_day, pending):
                if value is None:
                    status['problems'].append(f'ESPN history {day} unavailable or malformed ({error})')
                else:
                    records[day] = value
                    _write(cache_dir / (day + '.json'), value)
        observed = clock().astimezone(timezone.utc)
        status.update(observed_at=_stamp(observed), requested_dates=len(season['days']),
                      verified_dates=len(records), missing_dates=[d for d in season['days'] if d not in records])
        if status['missing_dates']:
            status['status'] = 'incomplete'
        else:
            results = [r for day in season['days'] for r in records[day]['results']]
            ratings = _ratings(results, observed)
            team_ids = {}
            for result in results:
                for side in ('home', 'away'):
                    canonical, native = result[side + '_id'], result[side + '_provider_id']
                    if canonical in team_ids and team_ids[canonical] != native:
                        raise ValueError('ESPN canonical/native team identity conflict')
                    team_ids[canonical] = native
            snapshot = {'schema': SCHEMA, 'method': METHOD, 'observed_at': _stamp(observed),
                        'availability': 'genuinely_retrieved_before_future_decision_only',
                        'season': season, 'coverage_scope': status['coverage_scope'],
                        'source_records': [records[d] for d in season['days']],
                        'ratings': ratings, 'result_count': len(results), 'team_provider_ids': team_ids}
            snapshot['snapshot_sha256'] = _digest(snapshot)
            relative = 'data/f4/strength/snapshots/' + snapshot['snapshot_sha256'] + '.json'
            path = root / relative
            if not path.exists():
                _write(path, snapshot)
            elif _read(path) != snapshot:
                raise ValueError('immutable strength snapshot content conflict')
            status.update(status='ready', snapshot_path=relative,
                          snapshot_sha256=snapshot['snapshot_sha256'], result_count=len(results),
                          teams=len(ratings), teams_with_five_games=sum(x['prior_games'] >= 5 for x in ratings.values()))
            status['requested_fixtures'] = len(fixtures)
            status['rated_requested_fixtures'] = sum(
                f.get('home_id') in ratings and f.get('away_id') in ratings
                and min(ratings[f['home_id']]['prior_games'], ratings[f['away_id']]['prior_games']) >= 5
                for f in fixtures)
    except Exception as exc:
        status['problems'].append(f'ESPN strength refresh unavailable or malformed ({type(exc).__name__})')
        status['observed_at'] = _stamp(clock())
    _write(latest_path, status)
    return status


def ratings_for_fixture(root, fixture, decision, *, max_age_hours=48):
    """Read latest intact snapshot genuinely available before the decision.

    No network, no historical backfill, no use of a snapshot from another season.
    A failed latest refresh does not erase intact older eligible snapshots.
    """
    if decision.utcoffset() is None:
        raise ValueError('strength decision requires timezone')
    decision = decision.astimezone(timezone.utc)
    if not isinstance(max_age_hours, (int, float)) or isinstance(max_age_hours, bool) or not math.isfinite(max_age_hours) or max_age_hours <= 0:
        raise ValueError('strength age limit must be finite and positive')
    try:
        kickoff = _instant(fixture['kickoff_at'])
        if fixture.get('league') != 'england-premier' or fixture.get('provider') != 'espn' or decision >= kickoff:
            return None
        h, a = fixture['home_id'], fixture['away_id']
        if not isinstance(h, str) or not h or not isinstance(a, str) or not a or h == a:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    candidates = []
    for path in (Path(root) / 'data/f4/strength/snapshots').glob('*.json'):
        row = _read(path)
        try:
            if row is None or row.get('schema') != SCHEMA or row.get('method') != METHOD:
                continue
            body = {k: v for k, v in row.items() if k != 'snapshot_sha256'}
            sha = _digest(body)
            if row.get('snapshot_sha256') != sha or path.stem != sha:
                continue
            observed = _instant(row['observed_at'])
            if not 0 <= (decision - observed).total_seconds() <= max_age_hours * 3600:
                continue
            season = row['season']
            if not _instant(season['start_at']) <= kickoff <= _instant(season['end_at']):
                continue
            sources = row['source_records']
            if [v['day'] for v in sources] != season['days'] or any(
                    not _cached_day(v, v['day'], season['year'], observed) for v in sources):
                continue
            results = [r for record in sources for r in record['results']]
            calculated = _ratings(results, observed)
            if row.get('ratings') != calculated or row.get('result_count') != len(results):
                continue
            home, away = calculated[h], calculated[a]
            if min(home['prior_games'], away['prior_games']) < 5:
                continue
            candidates.append((observed, {'home': home['elo'], 'away': away['elo'],
                'observed_at': row['observed_at'], 'retrieved_at': row['observed_at'],
                'source': METHOD, 'prior_games': [home['prior_games'], away['prior_games']],
                'snapshot_sha256': sha, 'season_year': season['year'],
                'result_count': len(results), 'availability': row['availability'],
                'latest_result_kickoff_at': max(r['kickoff_at'] for r in results)}))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return max(candidates, key=lambda value: value[0])[1] if candidates else None
