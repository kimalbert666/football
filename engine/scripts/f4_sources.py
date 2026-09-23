"""Fresh, read-only f4 source adapters; no legacy cache reads or writes.

``fetch(url) -> dict`` may be injected for deterministic tests. The default
transport supplies Sporttery's required headers, but no browser UA to ESPN.
Only normalized fields and response hashes leave this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

ESPN_BASE = 'https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/'
SPORTTERY_URL = 'https://webapi.sporttery.cn/gateway/uniform/football/getMatchCalculatorV1.qry?channel=c'
LEAGUE = 'england-premier'
CHINA = timezone(timedelta(hours=8))
MAX_PENDING = 20
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504, 567}


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _instant(value, local=None):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('missing timestamp')
    parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        if local is None:
            raise ValueError('timestamp lacks timezone')
        parsed = parsed.replace(tzinfo=local)
    return parsed.astimezone(timezone.utc)


def _fetch(url):
    import requests
    headers = {}
    if url.startswith(SPORTTERY_URL.split('?')[0]):
        from sporttery_fetch import HEADERS
        headers = HEADERS
    # Three total attempts with bounded waits; authentication refusals are final.
    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, 'status_code', None)
            if status not in TRANSIENT_HTTP_STATUSES or attempt == 2:
                raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == 2:
                raise
        time.sleep(attempt + 1)


def _name(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split()) if isinstance(value, str) else ''


def _aliases(root):
    data = json.loads((Path(root) / 'data/01-teams/_aliases.json').read_text(encoding='utf-8'))
    teams = data.get('england')
    if not isinstance(teams, dict) or not teams:
        raise ValueError('England aliases missing')
    index = {}
    for canonical, aliases in teams.items():
        if not isinstance(aliases, dict):
            continue
        names = [canonical, *(v for k, v in aliases.items() if k != 'league' and isinstance(v, str))]
        names += [v for v in aliases.get('variants', []) if isinstance(v, str)]
        for name in names:
            index.setdefault(_name(name), set()).add(canonical)
    return index


def _resolve(names, aliases):
    matches = set()
    for name in names:
        matches.update(aliases.get(_name(name), set()))
    return next(iter(matches)) if len(matches) == 1 else None


def _fixture(provider, event_id, home='', away=''):
    return {'match_id': f'{provider}:{event_id}', 'source_event_id': event_id,
            'provider': provider, 'league': LEAGUE, 'home_id': None, 'away_id': None,
            'home': home, 'away': away, 'kickoff_at': None, 'status': 'unknown',
            'score': None, 'market': None, 'problems': []}


def _espn_event(event, aliases, expected_id=None):
    if not isinstance(event, dict) or not str(event.get('id', '')).isdigit():
        raise ValueError('ESPN event missing numeric id')
    event_id = str(event['id'])
    row = _fixture('espn', event_id)
    issues = row['problems']
    competitions = event.get('competitions')
    if not isinstance(competitions, list) or len(competitions) != 1 or not isinstance(competitions[0], dict):
        issues.append('ESPN event must have exactly one competition')
        return row
    comp = competitions[0]
    identity_bad = str(comp.get('id', '')) != event_id or (expected_id is not None and event_id != expected_id)
    competitors = comp.get('competitors')
    competitors = competitors if isinstance(competitors, list) else []
    sides = {}
    for side in ('home', 'away'):
        choices = [c for c in competitors if isinstance(c, dict) and c.get('homeAway') == side]
        if len(choices) != 1:
            issues.append(f'ESPN {side} competitor missing or ambiguous')
            identity_bad = True
            continue
        person = choices[0]
        team = person.get('team') if isinstance(person.get('team'), dict) else {}
        names = [team.get(k) for k in ('displayName', 'name', 'shortDisplayName', 'location')]
        row[side] = next((n for n in names if isinstance(n, str) and n), '')
        row[side + '_id'] = _resolve(names, aliases)
        if row[side + '_id'] is None:
            issues.append(f'ESPN {side} alias unresolved or ambiguous: {row[side]}')
        if person.get('id') is not None and team.get('id') is not None and str(person['id']) != str(team['id']):
            identity_bad = True
        sides[side] = person
    if len(competitors) != 2 or (row['home_id'] is not None and row['home_id'] == row['away_id']):
        identity_bad = True
    try:
        if comp.get('timeValid', event.get('timeValid')) is not True:
            raise ValueError('ESPN kickoff time not confirmed')
        kickoff = _instant(comp.get('date') or event.get('date'))
        if event.get('date') and _instant(event['date']) != kickoff:
            raise ValueError('ESPN event/competition kickoff conflict')
        row['kickoff_at'] = _stamp(kickoff)
    except (TypeError, ValueError) as exc:
        issues.append(str(exc))
    status = comp.get('status', event.get('status'))
    kind = status.get('type', {}) if isinstance(status, dict) else {}
    kind = kind if isinstance(kind, dict) else {}
    name = str(kind.get('name', '')).upper()
    if 'POSTPON' in name:
        row['status'] = 'postponed'
    elif 'CANCEL' in name:
        row['status'] = 'cancelled'
    elif name == 'STATUS_FULL_TIME' and kind.get('completed') is True:
        # An explicit FT in this league is regulation time. AET/penalties are
        # deliberately never inferred from a generic completed flag.
        try:
            if status.get('period', 2) not in (1, 2):
                raise ValueError('non-regulation period')
            score = [sides[s]['score'] for s in ('home', 'away')]
            if any(isinstance(v, bool) or not str(v).isdigit() for v in score):
                raise ValueError('invalid full-time score')
            row['score'] = [int(v) for v in score]
            row['status'] = 'completed'
        except (KeyError, ValueError, TypeError):
            issues.append('ESPN completed result lacks verified 90-minute score')
    elif name == 'STATUS_SCHEDULED' and kind.get('completed') is False:
        row['status'] = 'scheduled'
    elif kind.get('state') == 'in' and kind.get('completed') is False:
        row['status'] = 'live'
    else:
        issues.append('ESPN status is not a verified regulation-time result or supported state')
    if identity_bad:
        issues.append('ESPN fixture identity conflict')
        row.update(home_id=None, away_id=None, status='unknown', score=None)
    if row['home_id'] is None or row['away_id'] is None or row['kickoff_at'] is None:
        row['score'] = None
        if row['status'] == 'completed':
            row['status'] = 'unknown'
    return row


def _sporttery_rows(payload):
    if payload.get('success') is not True:
        raise ValueError('Sporttery success flag absent or false')
    value = payload.get('value')
    if not isinstance(value, dict) or not isinstance(value.get('matchInfoList'), list):
        raise ValueError('Sporttery matchInfoList missing')
    rows = []
    for group in value['matchInfoList']:
        if not isinstance(group, dict) or not isinstance(group.get('subMatchList'), list):
            raise ValueError('Sporttery day block malformed')
        if any(not isinstance(row, dict) for row in group['subMatchList']):
            raise ValueError('Sporttery match row malformed')
        rows.extend(group['subMatchList'])
    return rows


def _sporttery_fixture(raw, aliases, captured_at):
    event_id = str(raw.get('matchId', ''))
    if not event_id.isdigit():
        raise ValueError('Sporttery matchId missing')
    def team_name(side):
        value = raw.get(side + 'TeamAbbName') or raw.get(side + 'TeamName') or ''
        return value.get('name', '') if isinstance(value, dict) else str(value)
    row = _fixture('sporttery', event_id, team_name('home'), team_name('away'))
    for side in ('home', 'away'):
        row[side + '_id'] = _resolve([row[side]], aliases)
        if row[side + '_id'] is None:
            row['problems'].append(f'Sporttery {side} alias unresolved or ambiguous: {row[side]}')
    try:
        kickoff = _instant(str(raw.get('matchDate', '')) + 'T' + str(raw.get('matchTime', '')), CHINA)
        row['kickoff_at'] = _stamp(kickoff)
    except (TypeError, ValueError):
        row['problems'].append('Sporttery kickoff missing or invalid')
        return row
    try:
        if kickoff <= _instant(captured_at):
            raise ValueError('Sporttery kickoff is not in the future')
        if raw.get('matchStatus') != 'Selling' or str(raw.get('sellStatus')) != '1':
            raise ValueError('Sporttery match is not on sale')
        pools = raw.get('poolList')
        pools = pools if isinstance(pools, list) else []
        had_pools = [p for p in pools if isinstance(p, dict) and p.get('poolCode') == 'HAD']
        if len(had_pools) != 1 or had_pools[0].get('poolStatus') != 'Selling':
            raise ValueError('Sporttery HAD pool is closed or ambiguous')
        had = raw.get('had')
        if not isinstance(had, dict):
            raise ValueError('Sporttery HAD quote missing')
        original = [had.get(k) for k in ('h', 'd', 'a')]
        if any(isinstance(v, bool) for v in original):
            raise ValueError('Sporttery HAD odds invalid')
        odds = [float(v) for v in original]
        if any(not math.isfinite(v) or v <= 1 for v in odds):
            raise ValueError('Sporttery HAD odds invalid')
        published = None
        if had.get('updateDate') and had.get('updateTime'):
            try:
                published = _instant(str(had['updateDate']) + 'T' + str(had['updateTime']), CHINA)
            except ValueError:
                row['problems'].append('Sporttery quote change timestamp unparseable')
        if published and published > _instant(captured_at):
            raise ValueError('Sporttery quote change timestamp is in the future')
        pool = had_pools[0]
        if pool.get('poolCloseDate') and pool.get('poolCloseTime'):
            if _instant(str(pool['poolCloseDate']) + 'T' + str(pool['poolCloseTime']), CHINA) <= _instant(captured_at):
                raise ValueError('Sporttery HAD pool closing time has passed')
        if row['home_id'] is None or row['away_id'] is None or row['home_id'] == row['away_id']:
            raise ValueError('Sporttery fixture identity unresolved')
        row['market'] = {'odds': odds, 'captured_at': captured_at,
                         'published_at': _stamp(published) if published else None,
                         'source': 'sporttery', 'url': SPORTTERY_URL, 'provider_event_id': event_id}
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        row['problems'].append(str(exc))
    return row


def collect(root, now, include_market=True, *, pending=None, fetch=None,
            lookahead_days=1, lookback_days=0):
    """Collect the complete daily EPL calendar and optionally match fresh HAD prices.

    ``now`` selects the query window. Capture timestamps always use the real UTC
    clock after the response arrives, never the supplied scheduling instant.
    Sporttery-only fallback rows stay unknown until an ESPN identity is verified.
    """
    if now.utcoffset() is None:
        raise ValueError('now requires a timezone')
    if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 31
           for v in (lookahead_days, lookback_days)):
        raise ValueError('query day counts must be integers between 0 and 31')
    now = now.astimezone(timezone.utc)
    fetch = fetch or _fetch
    sources, problems, fixtures = [], [], {}
    complete = True
    try:
        aliases = _aliases(root)
    except (OSError, TypeError, ValueError):
        aliases = {}
        problems.append('England team alias registry is unavailable or malformed')
        complete = False

    def get(name, url, parse):
        source = {'name': name, 'status': 'error', 'url': url, 'captured_at': None, 'content_sha256': None}
        try:
            payload = fetch(url)
            source['captured_at'] = _stamp(datetime.now(timezone.utc))
            if not isinstance(payload, dict):
                raise ValueError('source response is not an object')
            source['content_sha256'] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                                                allow_nan=False).encode()).hexdigest()
            result = parse(payload, source['captured_at'])
            source['status'] = 'ok'
            return result
        except Exception as exc:
            # Do not persist raw bodies, credentials, or provider error messages.
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            detail = type(exc).__name__
            if type(status) is int and 100 <= status <= 599:
                source['http_status'] = status
                detail += f'; HTTP {status}'
            problems.append(f'{name}: unavailable or malformed ({detail})')
            return None
        finally:
            source['captured_at'] = source['captured_at'] or _stamp(datetime.now(timezone.utc))
            sources.append(source)

    def merge(row):
        nonlocal complete
        previous = fixtures.get(row['match_id'])
        if previous and any(previous[k] != row[k] for k in ('home_id', 'away_id', 'kickoff_at', 'status', 'score')):
            row.update(home_id=None, away_id=None, status='unknown', score=None, market=None)
            row['problems'].append('ESPN duplicate event identity or state conflict')
        fixtures[row['match_id']] = row
        if row['problems']:
            complete = False
            problems.extend(f"{row['match_id']}: {p}" for p in row['problems'])

    start = now.date() - timedelta(days=lookback_days)
    end = now.date() + timedelta(days=lookahead_days)
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        url = ESPN_BASE + 'scoreboard?' + urlencode({'dates': day.strftime('%Y%m%d')})
        def parse_calendar(payload, captured):
            leagues = payload.get('leagues')
            if not isinstance(leagues, list) or not any(isinstance(l, dict) and l.get('slug') == 'eng.1' for l in leagues):
                raise ValueError('ESPN EPL league missing')
            if not isinstance(payload.get('events'), list):
                raise ValueError('ESPN events missing')
            return [_espn_event(e, aliases) for e in payload['events']]
        rows = get('espn-calendar', url, parse_calendar)
        if rows is None:
            complete = False
        else:
            for row in rows:
                merge(row)

    wanted = {}
    for record in pending or []:
        try:
            event_id = str(record['source_event_id'])
            if record.get('provider') != 'espn' or record['match_id'] != 'espn:' + event_id or not event_id.isdigit():
                raise ValueError('pending identity invalid')
            if record.get('league') != LEAGUE or _instant(record['kickoff_at']) >= now:
                continue
            if record['match_id'] not in fixtures:
                wanted[event_id] = record
        except (KeyError, TypeError, ValueError):
            problems.append('Pending fixture has unsupported or inconsistent provider identity')
            complete = False
    if len(wanted) > MAX_PENDING:
        problems.append(f'Pending ESPN lookups truncated to {MAX_PENDING} of {len(wanted)}')
        complete = False
    for event_id, record in list(wanted.items())[:MAX_PENDING]:
        url = ESPN_BASE + 'summary?' + urlencode({'event': event_id})
        def parse_summary(payload, captured):
            header = payload.get('header')
            if not isinstance(header, dict) or not isinstance(header.get('league'), dict) or header['league'].get('slug') != 'eng.1':
                raise ValueError('ESPN summary league mismatch')
            row = _espn_event(header, aliases, expected_id=event_id)
            if any(record.get(k) != row[k] for k in ('home_id', 'away_id')):
                row.update(home_id=None, away_id=None, status='unknown', score=None)
                row['problems'].append('ESPN summary identity differs from pending fixture')
            return row
        row = get('espn-result', url, parse_summary)
        if row is None:
            complete = False
        else:
            merge(row)

    calendar_complete = complete
    config_path = Path(root) / 'data/f4/config.json'
    config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    odds_source = config.get('odds_source', 'sporttery')
    if odds_source not in ('sporttery', 'crown'):
        raise ValueError('Unsupported fixed odds source')
    if include_market and odds_source == 'crown':
        from f4_crown import attach_crown
        queried_at = datetime.now(timezone.utc)
        due = []
        for row in fixtures.values():
            if row['status'] != 'scheduled' or not row['kickoff_at'] or not row['home_id'] or not row['away_id']:
                continue
            minutes = (_instant(row['kickoff_at']) - queried_at).total_seconds() / 60
            old_due = any(abs(minutes - h) <= config.get('horizon_tolerance_minutes', 20)
                          for h in config.get('horizons_minutes', [180, 60]))
            ah_cfg = config.get('asian_handicap', {})
            ah_due = ah_cfg.get('enabled') and any(abs(minutes - int(h)) <= tolerance
                                                 for h, tolerance in ah_cfg.get('windows', {}).items())
            if minutes > 0 and (old_due or ah_due):
                due.append(row)
        crown = attach_crown(root, due, queried_at)
        sources.extend(crown.get('sources', []))
        problems.extend(crown.get('problems', []))
        for match_id, market in crown.get('markets', {}).items():
            if match_id in fixtures and fixtures[match_id]['status'] == 'scheduled':
                fixtures[match_id]['market'] = market
    elif include_market:
        quotes = get('sporttery', SPORTTERY_URL,
                     lambda payload, captured: ([_sporttery_fixture(r, aliases, captured)
                         for r in _sporttery_rows(payload)
                         if r.get('leagueAbbName') == '英超' or r.get('leagueCode') == 'EPL']))
        if quotes is not None:
            matches = {}
            for quote in quotes:
                candidates = []
                if quote['home_id'] and quote['away_id'] and quote['kickoff_at']:
                    candidates = [row for row in fixtures.values()
                        if row['provider'] == 'espn' and row['home_id'] == quote['home_id'] and row['away_id'] == quote['away_id']
                        and row['kickoff_at'] and abs((_instant(row['kickoff_at']) - _instant(quote['kickoff_at'])).total_seconds()) <= 300]
                if len(candidates) == 1:
                    matches.setdefault(candidates[0]['match_id'], []).append(quote)
                elif len(candidates) > 1:
                    problems.append(f"{quote['match_id']}: Sporttery quote maps to multiple ESPN events")
                elif not calendar_complete:
                    quote['problems'].append('Sporttery fallback: independent ESPN calendar identity unverified')
                    fixtures[quote['match_id']] = quote
                    problems.extend(f"{quote['match_id']}: {p}" for p in quote['problems'])
                else:
                    problems.append(f"{quote['match_id']}: Sporttery EPL row has no exact ESPN fixture match")
            for match_id, candidates in matches.items():
                row = fixtures[match_id]
                if len(candidates) != 1:
                    row['problems'].append('Multiple Sporttery quotes match this ESPN fixture')
                else:
                    row['problems'].extend(candidates[0]['problems'])
                    if row['status'] == 'scheduled':
                        row['market'] = candidates[0]['market']
                problems.extend(f'{match_id}: {p}' for p in row['problems'])
    return {'captured_at': _stamp(datetime.now(timezone.utc)), 'sources': sources,
            'fixtures': sorted(fixtures.values(), key=lambda row: (row['kickoff_at'] or '', row['match_id'])),
            'coverage': {'complete': calendar_complete,
                         'scope': {'league': LEAGUE, 'from': start.isoformat(), 'through': end.isoformat(),
                                   'provider': 'espn', 'pending_lookup_limit': MAX_PENDING},
                         'reason': 'Independent daily EPL calendar verified' if calendar_complete
                                   else 'Independent EPL calendar or pending identities could not be fully verified'},
            'problems': list(dict.fromkeys(problems))}
