"""Read public Titan007 Crown 90-minute 1X2 quotes without executing JavaScript.

The HTML schedule is UTC+8; the independently linked data JavaScript uses UTC.
Only the selected Crown row leaves this adapter. A displayed quote does not
prove that the bookmaker is currently accepting bets (that status is absent).
``fetch(url)`` can return text/bytes or {body, captured_at} for offline tests.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from f4_sources import _aliases, _instant, _resolve, _stamp

INDEX_URL = 'https://1x2.titan007.com/index_vip.aspx'
TOMORROW_URL = 'https://1x2.titan007.com/TomorrowOdds.aspx'
COMPANY_URL = 'https://1x2.titan007.com/Company.js'
DATA_BASE = 'https://1x2d.titan007.com/'
CHINA = timezone(timedelta(hours=8))
MAX_MATCH_REQUESTS = 20


def _fetch(url):
    import requests
    # No authentication, browser impersonation, proxies, or redirect fallback.
    for attempt in range(2):
        try:
            response = requests.get(url, timeout=25, allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError(f'HTTP {response.status_code}: redirects are not followed')
            return {'body': response.content, 'captured_at': _stamp(datetime.now(timezone.utc))}
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt:
                raise


def _read(url, fetch, sources):
    metadata = {'name': 'titan007-crown', 'url': url, 'status': 'error'}
    try:
        received = fetch(url)
        if isinstance(received, dict):
            body = received['body']
            captured = _instant(received['captured_at'])
        else:
            body, captured = received, datetime.now(timezone.utc)
        if not isinstance(body, (str, bytes)):
            raise ValueError('response must be text or bytes')
        raw = body.encode('utf-8') if isinstance(body, str) else body
        text = body if isinstance(body, str) else body.decode(
            'utf-8-sig' if url.startswith(DATA_BASE) else 'gb18030')
        metadata.update(status='ok', captured_at=_stamp(captured),
                        content_sha256=hashlib.sha256(raw).hexdigest())
        return text, captured, metadata['content_sha256']
    except Exception as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if isinstance(status, int):
            metadata['http_status'] = status
        metadata['error'] = type(exc).__name__ + (f' HTTP {status}' if isinstance(status, int) else '')
        raise RuntimeError(metadata['error']) from None
    finally:
        sources.append(metadata)


def _plain(value):
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', ' ', value)).split())


def parse_index(text, aliases):
    """Parse only EPL schedule identities; never use the page's average odds."""
    if not re.search(r'<table\b[^>]*\bid\s*=\s*[\"\x27]?table_schedule\b', text, re.I):
        raise ValueError('Crown schedule table is missing')
    rows = []
    for raw in re.findall(r'<tr\b[^>]*>.*?</tr>', text, re.I | re.S):
        cells = re.findall(r'<td\b[^>]*>(.*?)</td>', raw, re.I | re.S)
        if len(cells) < 13 or _plain(cells[1]) != '英超':
            continue
        matches = re.findall(r'(?:/|\b)Oddslist/(\d+)\.htm', cells[12], re.I)
        leagues = re.findall(r'SclassID=(\d+)', cells[1], re.I)
        kickoff_text = _plain(cells[2])
        if len(set(matches)) != 1 or len(set(leagues)) != 1:
            raise ValueError('EPL index identity missing or ambiguous')
        try:
            kickoff = datetime.strptime(kickoff_text, '%y-%m-%d %H:%M').replace(tzinfo=CHINA)
        except ValueError as exc:
            raise ValueError('EPL index kickoff malformed') from exc
        # Ranking badges inside <font> are not part of a team's name.
        home = _plain(re.sub(r'<font\b[^>]*>.*?</font>', '', cells[3], flags=re.I | re.S))
        away = _plain(re.sub(r'<font\b[^>]*>.*?</font>', '', cells[11], flags=re.I | re.S))
        rows.append({'provider_event_id': matches[0], 'league_id': leagues[0],
                     'home_id': _resolve([home], aliases), 'away_id': _resolve([away], aliases),
                     'home': home, 'away': away, 'kickoff_at': _stamp(kickoff)})
    return rows


def _scalar(text, name, integer=False):
    token = r'\d+' if integer else r'"(?:[^"\\]|\\.)*"'
    values = re.findall(r'\bvar\s+' + re.escape(name) + r'\s*=\s*(' + token + r')\s*;', text)
    if len(values) != 1:
        raise ValueError(f'{name} missing or duplicated')
    return int(values[0]) if integer else json.loads(values[0])


def parse_js_time(value):
    # The public renderer uses Date.UTC. The month token is literal MM-1.
    match = re.fullmatch(r'(\d{4}),(\d{1,2})-1,(\d{1,2}),(\d{1,2}),(\d{1,2}),(\d{1,2})', value)
    if not match:
        raise ValueError('provider UTC timestamp malformed')
    return datetime(*map(int, match.groups()), tzinfo=timezone.utc)


def parse_quote(text, row, fixture, aliases, captured_at, url, digest):
    """Validate identity twice and take only the explicit latest Crown row."""
    captured = _instant(captured_at)
    if fixture.get('league') != 'england-premier' or fixture.get('status') != 'scheduled':
        raise ValueError('fixture is not a scheduled EPL match')
    if _scalar(text, 'ScheduleID', True) != int(row['provider_event_id']):
        raise ValueError('provider match ID conflict')
    if str(_scalar(text, 'sclassId', True)) != row['league_id']:
        raise ValueError('provider league ID conflict')
    if (_scalar(text, 'matchname_cn') != '英超'
            or _scalar(text, 'matchname').casefold() not in ('english premier league', 'england premier league')
            or _scalar(text, 'sclassKind', True) != 1):
        raise ValueError('provider league is not EPL')
    if _scalar(text, 'ifShow', True) != 1:
        raise ValueError('provider quote page is disabled')
    if _scalar(text, 'neutrality') != '0':
        raise ValueError('neutral venue identity requires separate validation')
    for side, prefix in (('home', 'home'), ('away', 'guest')):
        identity = _resolve([_scalar(text, prefix + 'team'), _scalar(text, prefix + 'team_cn')], aliases)
        if identity is None or identity != fixture.get(side + '_id'):
            raise ValueError(f'provider {side} team conflict or unresolved alias')
        if row.get(side + '_id') not in (None, identity):
            raise ValueError(f'index and detail {side} conflict')
    if _scalar(text, 'hometeamID', True) == _scalar(text, 'guestteamID', True):
        raise ValueError('identical provider team IDs')
    kickoff = parse_js_time(_scalar(text, 'MatchTime'))
    espn_kickoff, index_kickoff = _instant(fixture['kickoff_at']), _instant(row['kickoff_at'])
    if abs((kickoff - espn_kickoff).total_seconds()) > 300 or abs((kickoff - index_kickoff).total_seconds()) > 300:
        raise ValueError('provider kickoff conflict')
    if captured >= min(kickoff, espn_kickoff, index_kickoff):
        raise ValueError('quote fetched at or after kickoff')
    arrays = re.findall(r'\bvar\s+game\s*=\s*Array\((.*?)\)\s*;', text, re.S)
    if len(arrays) != 1:
        raise ValueError('provider quote array missing or duplicated')
    # JSON parses data only: JavaScript expressions are never evaluated.
    rows = json.loads('[' + arrays[0] + ']')
    if any(not isinstance(value, str) for value in rows):
        raise ValueError('non-string quote row')
    crown = [value.split('|') for value in rows if value.split('|')[0] == '545']
    if len(crown) != 1 or len(crown[0]) < 24:
        raise ValueError('Crown quote missing, ambiguous or malformed')
    quote = crown[0]
    if quote[2] != 'Crown' or quote[21] != 'Crow*' or quote[23] != '0':
        raise ValueError('Crown company identity conflict')
    odds = [float(value) for value in quote[10:13]]
    if len(odds) != 3 or any(not math.isfinite(value) or value <= 1 for value in odds):
        raise ValueError('invalid latest Crown odds; opening odds are not a fallback')
    published = parse_js_time(quote[20])
    if published > captured or published >= min(kickoff, espn_kickoff, index_kickoff):
        raise ValueError('Crown update time is future or at/after kickoff')
    return {'bookmaker': 'crown', 'company_id': 545, 'market': '90min_1x2',
            'odds': odds, 'captured_at': _stamp(captured), 'published_at': _stamp(published),
            'source': 'titan007-crown', 'url': url, 'provider_event_id': row['provider_event_id'],
            'provider_quote_id': quote[1], 'content_sha256': digest,
            'provider_kickoff_at': _stamp(kickoff), 'market_status': 'not_exposed',
            'provenance': 'Titan007 public display attributed to Crown; not direct bookmaker API'}


def attach_crown(root, fixtures, now, *, fetch=None):
    """Attach verified single-bookmaker quotes to due ESPN EPL fixtures only."""
    if now.utcoffset() is None:
        raise ValueError('now requires a timezone')
    result = {'markets': {}, 'sources': [], 'problems': []}
    transport = fetch or _fetch
    try:
        aliases = _aliases(Path(root))
    except (OSError, ValueError, TypeError):
        result['problems'].append('Crown team aliases are unavailable or malformed')
        return result
    eligible = []
    config_path = Path(root) / 'data/f4/config.json'
    config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    windows = {180: 20, 60: 20}
    ah = config.get('asian_handicap', {})
    if ah.get('enabled'):
        windows.update({int(h): tolerance for h, tolerance in ah.get('windows', {}).items()})
    for fixture in fixtures:
        try:
            remaining = (_instant(fixture.get('kickoff_at')) - now).total_seconds() / 60
            if (fixture.get('provider') == 'espn' and fixture.get('league') == 'england-premier'
                    and fixture.get('status') == 'scheduled' and fixture.get('home_id')
                    and fixture.get('away_id') and fixture['home_id'] != fixture['away_id']
                    and remaining > 0 and any(abs(remaining - window) <= tolerance for window, tolerance in windows.items())):
                eligible.append(fixture)
        except (ValueError, TypeError):
            continue
    try:
        company, _, _ = _read(COMPANY_URL, transport, result['sources'])
        if not re.search(r"company\.aspx\?id=545&company=Crow\*['\"]>Crow\*", company):
            raise ValueError('public company mapping no longer confirms Crown ID 545')
        index, _, _ = _read(INDEX_URL, transport, result['sources'])
        rows = parse_index(index, aliases)
        if eligible:
            tomorrow, _, _ = _read(TOMORROW_URL, transport, result['sources'])
            rows.extend(parse_index(tomorrow, aliases))
    except Exception as exc:
        result['problems'].append(f'Crown source unavailable: {type(exc).__name__}: {str(exc)[:160]}')
        return result
    if not eligible:
        result['sources'].append({'name': 'titan007-crown-quotes', 'status': 'not_requested',
                                  'reason': 'No confirmed EPL fixture in a capture window'})
        return result
    by_id, ambiguous = {}, set()
    for row in rows:
        identifier = row['provider_event_id']
        if identifier in by_id and row != by_id[identifier]:
            ambiguous.add(identifier)
        by_id[identifier] = row
    used, found = 0, {}
    for identifier, row in by_id.items():
        if identifier in ambiguous:
            result['problems'].append(f'Crown index identity conflict: {identifier}')
            continue
        possible = [fixture for fixture in eligible
                    if abs((_instant(fixture['kickoff_at']) - _instant(row['kickoff_at'])).total_seconds()) <= 300
                    and all(row.get(side + '_id') in (None, fixture[side + '_id']) for side in ('home', 'away'))]
        if not possible:
            continue
        if used >= MAX_MATCH_REQUESTS:
            result['problems'].append('Crown EPL detail request limit reached; remaining quotes missing')
            break
        used += 1
        url = DATA_BASE + identifier + '.js'
        try:
            data, captured, digest = _read(url, transport, result['sources'])
            matches = []
            errors = []
            for fixture in possible:
                try:
                    quote = parse_quote(data, row, fixture, aliases, _stamp(captured), url, digest)
                    matches.append((fixture['match_id'], quote))
                except (ValueError, TypeError, KeyError) as exc:
                    errors.append(str(exc))
            if len(matches) != 1:
                raise ValueError('detail match not unique: ' + '; '.join(sorted(set(errors)))[:200])
            key, quote = matches[0]
            found.setdefault(key, []).append(quote)
        except Exception as exc:
            result['problems'].append(f'Crown {identifier}: {type(exc).__name__}: {str(exc)[:200]}')
    for fixture in eligible:
        key = fixture['match_id']
        quotes = found.get(key, [])
        if len(quotes) == 1:
            result['markets'][key] = quotes[0]
        else:
            result['problems'].append(f'Crown quote missing or ambiguous for {key}')
    return result
