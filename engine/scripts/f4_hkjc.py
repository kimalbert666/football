"""Read-only HKJC public website quotes, independent of InferSports.

The approved match-list query is from the MIT-licensed hkjc-api package;
see docs/licenses/hkjc-api.txt. No login, transaction or wagering endpoints.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from f4_sources import _aliases, _resolve, _instant, _stamp

URL = 'https://info.cld.hkjc.com/graphql/base/'
QUERY_PATH = Path(__file__).with_name('f4_hkjc_matchlist.graphql')
VARIABLES = {'fbOddsTypes': ['HAD', 'HDC'], 'fbOddsTypesM': ['HAD', 'HDC'],
             'startDate': None, 'endDate': None, 'tournIds': None, 'matchIds': None,
             'featuredMatchesOnly': False, 'frontEndIds': None, 'earlySettlementOnly': False,
             'showAllMatch': False, 'startIndex': None, 'endIndex': None}


def fetch():
    import requests
    for attempt in range(2):
        try:
            response = requests.post(URL, json={'query': QUERY_PATH.read_text(encoding='utf-8'),
                                               'variables': VARIABLES}, timeout=20, allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError('HKJC non-200 or redirected response')
            return {'payload': response.json(), 'headers': dict(response.headers),
                    'captured_at': _stamp(datetime.now(timezone.utc))}
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt:
                raise


def validate_response(received):
    payload = received['payload']
    if not isinstance(payload, dict) or payload.get('errors'):
        raise ValueError('HKJC query returned errors; no partial response accepted')
    rows = (payload.get('data') or {}).get('matches')
    if not isinstance(rows, list):
        raise ValueError('HKJC match list missing')
    headers = {k.lower(): v for k, v in received['headers'].items()}
    server = parsedate_to_datetime(headers['date'])
    if server.utcoffset() is None:
        raise ValueError('HKJC response timezone missing')
    age = (_instant(received['captured_at']) - server).total_seconds()
    cache_age = float(headers.get('age', 0))
    max_age = re.search(r'(?:^|[, ])max-age=(\d+)', headers.get('cache-control', ''))
    if (not -5 <= age <= 120 or not math.isfinite(cache_age) or not 0 <= cache_age <= 60
            or not max_age or int(max_age[1]) > 60):
        raise ValueError('HKJC HTTP snapshot freshness unverified')
    return rows, _stamp(server)


def line_value(condition):
    if not isinstance(condition, str) or not re.fullmatch(r'[+-]?\d+(?:\.\d+)?(?:/[+-]?\d+(?:\.\d+)?)?', condition):
        raise ValueError('unrecognized handicap condition')
    components = [float(s) for s in condition.split('/')]
    if any(not math.isfinite(x) or not math.isclose(x * 2, round(x * 2), abs_tol=1e-9) for x in components):
        raise ValueError('handicap components must be whole or half goals')
    if len(components) == 2 and not math.isclose(abs(components[0] - components[1]), .5):
        raise ValueError('split handicap components must be adjacent')
    return sum(components) / len(components)


def prices(line, sides):
    result = {}
    if line.get('status') != 'AVAILABLE':
        raise ValueError('line unavailable')
    for item in line.get('combinations', []):
        side = item.get('str')
        if side not in sides or side in result or item.get('status') != 'AVAILABLE':
            raise ValueError('selection unavailable or ambiguous')
        if item.get('selections') and [s.get('str') for s in item['selections']] != [side]:
            raise ValueError('selection identity conflicts')
        value = item.get('currentOdds')
        if isinstance(value, bool):
            raise ValueError('numeric decimal price required')
        value = float(value)
        if not math.isfinite(value) or value <= 1:
            raise ValueError('invalid decimal price')
        result[side] = value
    if set(result) != set(sides):
        raise ValueError('selection missing')
    return [result[s] for s in sides]


def matches_fixture(row, fixture, aliases):
    if (fixture.get('provider') != 'espn' or fixture.get('league') != 'england-premier'
            or fixture.get('status') != 'scheduled' or not fixture.get('home_id')
            or not fixture.get('away_id') or fixture['home_id'] == fixture['away_id']):
        return False
    try:
        if row.get('tournament', {}).get('name_en') not in ('English Premier', 'English Premier League'):
            return False
        if row.get('status') != 'PREEVENT' or not row.get('id'):
            return False
        if abs((_instant(row['kickOffTime']) - _instant(fixture['kickoff_at'])).total_seconds()) > 60:
            return False
        for side in ('home', 'away'):
            team = row.get(side + 'Team', {})
            if (not team.get('id')
                    or _resolve([team.get('name_en'), team.get('name_ch')], aliases) != fixture[side + '_id']):
                return False
        return row['homeTeam']['id'] != row['awayTeam']['id']
    except (ValueError, KeyError, TypeError, AttributeError):
        # An unrelated malformed feed row must not suppress other verified games.
        return False


def parse_market(row, captured_at, response_at, content_hash):
    captured, kickoff = _instant(captured_at), _instant(row['kickOffTime'])
    if row.get('status') != 'PREEVENT' or captured >= kickoff:
        raise ValueError('not a pre-match event')
    if not {'HDC', 'HAD'} <= set(row.get('poolInfo', {}).get('sellingPools', [])):
        raise ValueError('required pools not currently selling')
    pools = {}
    for pool in row.get('foPools', []):
        kind = pool.get('oddsType')
        if kind not in ('HDC', 'HAD'):
            continue
        if kind in pools or pool.get('status') != 'SELLINGSTARTED':
            raise ValueError('pool unavailable or ambiguous')
        updated = _instant(pool['updateAt'])
        if updated > captured or updated >= kickoff:
            raise ValueError('pool update is future or after kickoff')
        # `inplay` denotes availability for live betting, not event state.
        # PREEVENT + kickoff + full-time HAD/HDC identify this pre-match quote.
        pools[kind] = pool
    if set(pools) != {'HAD', 'HDC'}:
        raise ValueError('both full-time markets required')
    had_lines = [line for line in pools['HAD'].get('lines', []) if line.get('status') == 'AVAILABLE']
    if len(had_lines) != 1:
        raise ValueError('ambiguous full-time 1X2')
    common = {'bookmaker': 'hkjc', 'captured_at': captured_at, 'snapshot_at': response_at,
              'source': 'hkjc-direct', 'url': URL, 'content_sha256': content_hash,
              'provider_event_id': row['id'], 'provider_kickoff_at': row['kickOffTime']}
    had = {**common, 'market': '90min_1x2', 'odds': prices(had_lines[0], ('H', 'D', 'A')),
           'published_at': _stamp(_instant(pools['HAD']['updateAt']))}
    lines = []
    for line in pools['HDC'].get('lines', []):
        if line.get('status') != 'AVAILABLE':
            continue
        lines.append({'home_handicap': line_value(line['condition']), 'odds': prices(line, ('H', 'A')),
                      'published_at': _stamp(_instant(pools['HDC']['updateAt'])),
                      'raw_condition': line['condition'], 'provider_main': line.get('main') is True})
    if not lines or len({r['home_handicap'] for r in lines}) != len(lines):
        raise ValueError('handicap lines absent or ambiguous')
    mains = [r for r in lines if r['provider_main']]
    if len(mains) > 1:
        raise ValueError('multiple provider main lines')
    chosen = mains[0] if mains else min(lines, key=lambda r: (abs(math.log(r['odds'][0] / r['odds'][1])), abs(r['home_handicap']), r['home_handicap']))
    return {**common, **chosen, 'market': '90min_asian_handicap', 'lines': lines,
            'line_selection': 'provider_main' if mains else 'closest_price_balance_not_provider_main',
            'market_1x2': had, 'opening_lines': [], 'market_status': 'official_provider_reports_selling',
            'freshness_evidence': 'current official HTTP response, cache <=60s, PREEVENT and selling pool status',
            'provenance': 'HKJC public website response; publication time is last pool change, no execution guarantee'}


def attach_hkjc(root, fixtures, now, transport=None, *, probe=False):
    result = {'markets': {}, 'sources': [], 'problems': []}
    if now.utcoffset() is None:
        raise ValueError('timezone required')
    eligible = []
    for fixture in fixtures:
        try:
            if (fixture.get('provider') == 'espn' and fixture.get('league') == 'england-premier'
                    and fixture.get('status') == 'scheduled' and fixture.get('home_id')
                    and fixture.get('away_id') and fixture['home_id'] != fixture['away_id']
                    and _instant(fixture.get('kickoff_at')) > now):
                eligible.append(fixture)
        except (ValueError, TypeError):
            continue
    if not eligible and not probe:
        return result
    metadata = {'name': 'hkjc-direct-ah', 'url': URL, 'status': 'error'}
    try:
        received = (transport or fetch)()
        rows, response_at = validate_response(received)
        sha = hashlib.sha256(json.dumps(received['payload'], sort_keys=True, allow_nan=False).encode()).hexdigest()
        aliases = _aliases(root)
        metadata.update(status='ok', captured_at=received['captured_at'], response_at=response_at,
                        content_sha256=sha, match_count=len(rows),
                        epl_match_count=sum(isinstance(r, dict) and isinstance(r.get('tournament'), dict)
                                            and r['tournament'].get('name_en') in ('English Premier', 'English Premier League') for r in rows))
        for fixture in eligible:
            choices = [row for row in rows if matches_fixture(row, fixture, aliases)]
            try:
                if len(choices) != 1:
                    raise ValueError('unique EPL identity not published or not matched')
                result['markets'][fixture['match_id']] = parse_market(choices[0], received['captured_at'], response_at, sha)
            except (ValueError, KeyError, TypeError) as exc:
                result['problems'].append(f"{fixture['match_id']}: HKJC AH unavailable ({str(exc)[:140]})")
    except Exception as exc:
        metadata.update(error=type(exc).__name__)
        result['problems'].append(f'HKJC independent source unavailable ({type(exc).__name__}: {str(exc)[:140]})')
    result['sources'].append(metadata)
    return result
