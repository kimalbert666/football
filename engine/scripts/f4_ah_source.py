"""Read publicly documented InferSports Crown quotes; never executes provider code.

Keyless Free tier: 200 requests/day/IP, 90 second delay. We enforce a lower
local daily ceiling even when GitHub runners change IP. Only finite selected
EPL/Crown observations and response hashes are retained, not the raw feed.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from f4_sources import _aliases, _resolve, _instant, _stamp

BASE = 'https://api.infersports.dev/v1'
LEAGUE_ID = 'lg_T9268DXK'  # Observed in the provider metadata, revalidated in every event.
MAX_DAILY = 180
MAX_FEED_AGE_SECONDS = 300
PROVIDER_NAMES = {'Nottm Forest': 'Nottingham Forest'}


def _fetch(url):
    import requests
    response = requests.get(url, timeout=20, allow_redirects=False)
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError('non-200 or redirected response')
    return {'payload': response.json(), 'captured_at': _stamp(datetime.now(timezone.utc))}


def _valid_price(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 1:
        raise ValueError('invalid decimal price')
    return float(value)


def _valid_line(value):
    if (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
            or not math.isclose(4 * value, round(4 * value), abs_tol=1e-9)):
        raise ValueError('invalid quarter-goal handicap')
    return float(value)


def _identity(event, fixture, aliases):
    if event.get('sport') != 'football' or event.get('status') != 'scheduled':
        return False
    league = event.get('league', {})
    if league.get('id') != LEAGUE_ID or league.get('name') not in ('ENPL', 'English Premier League', 'England Premier League'):
        return False
    if abs((_instant(event['scheduled_at']) - _instant(fixture['kickoff_at'])).total_seconds()) > 60:
        return False
    for side in ('home', 'away'):
        name = event[side + '_team']['name']
        if _resolve([name, PROVIDER_NAMES.get(name)], aliases) != fixture.get(side + '_id'):
            return False
    return event.get('home_team', {}).get('id') != event.get('away_team', {}).get('id')


def parse_quotes(payload, event, fixture, aliases, captured_at, url, content_sha256, opening=None, *, bookmaker='crown', health=None):
    if not _identity(event, fixture, aliases):
        raise ValueError('independent fixture identity mismatch')
    captured = _instant(captured_at)
    kickoff = min(_instant(event['scheduled_at']), _instant(fixture['kickoff_at']))
    if captured >= kickoff:
        raise ValueError('capture at or after kickoff')
    if payload.get('event_id') != event['id'] or payload.get('stale') is not False:
        raise ValueError('odds event conflict or stale snapshot')
    snapshot = _instant(payload['as_of'])
    age = (captured - snapshot).total_seconds()
    health_fresh = (isinstance(health, dict) and health.get('feed_live') is True
                    and health.get('as_of_stale') is False
                    and 0 <= (captured - _instant(health['as_of'])).total_seconds() <= MAX_FEED_AGE_SECONDS)
    if age < 0 or (age > MAX_FEED_AGE_SECONDS and not health_fresh):
        raise ValueError('feed snapshot old without fresh health confirmation, or from the future')
    lines, market_1x2 = {}, None
    for quote in payload.get('odds', []):
        if (quote.get('bookmaker') != bookmaker or quote.get('period') != 'full_time'
                or quote.get('status') != 'open' or quote.get('format') != 'decimal'):
            continue
        changed = _instant(quote['as_of'])
        if changed > snapshot or changed >= kickoff:
            raise ValueError('quote timestamp conflicts with snapshot')
        # Quote as_of is its last update, not necessarily the feed observation.
        # An unchanged open quote can be older than the current fresh snapshot.
        if quote.get('market_type') == '1x2':
            prices = [_valid_price(quote['prices'][s]) for s in ('home', 'draw', 'away')]
            if market_1x2 is not None:
                raise ValueError('ambiguous Crown 1X2')
            market_1x2 = {'bookmaker': bookmaker, 'market': '90min_1x2', 'odds': prices,
                          'captured_at': captured_at, 'published_at': _stamp(changed), 'source': 'infersports-' + bookmaker,
                          'url': url, 'provider_event_id': event['id'], 'snapshot_at': _stamp(snapshot)}
        elif quote.get('market_type') == 'asian_handicap':
            line = _valid_line(quote['line'])
            prices = [_valid_price(quote['prices'][s]) for s in ('home', 'away')]
            quarters = round(line * 4)
            if quarters % 2:
                components = quote.get('handicap_components')
                if (not isinstance(components, list) or len(components) != 2
                        or sorted(components) != [(quarters - 1) / 4, (quarters + 1) / 4]):
                    raise ValueError('quarter-line split conflicts with handicap')
            if line in lines:
                raise ValueError('duplicate Crown handicap line')
            lines[line] = {'home_handicap': line, 'odds': prices, 'published_at': _stamp(changed)}
    if not lines:
        raise ValueError('no open full-time Crown handicap quotes')
    opening_lines = []
    if opening is not None:
        if opening.get('event_id') != event['id']:
            raise ValueError('opening event conflict')
        for quote in opening.get('opening', []):
            if (quote.get('bookmaker') == bookmaker and quote.get('period') == 'full_time'
                    and quote.get('market_type') == 'asian_handicap' and quote.get('format') == 'decimal'):
                opened = _instant(quote['opened_at'])
                if opened > captured or opened >= kickoff:
                    raise ValueError('invalid declared opening time')
                opening_lines.append({'home_handicap': _valid_line(quote['line']),
                                      'odds': [_valid_price(quote['prices'][s]) for s in ('home', 'away')],
                                      'published_at': _stamp(opened), 'captured_at': opening.get('_captured_at', captured_at),
                                      'provenance': 'provider-reported opening; not independently certified'})
    # Fixed selection before seeing outcomes. Alternative lines are all kept;
    # this is expressly NOT a provider-certified main-line identifier.
    representative = min(lines.values(), key=lambda q: (abs(math.log(q['odds'][0] / q['odds'][1])),
                                                         abs(q['home_handicap']), q['home_handicap']))
    return {**representative, 'bookmaker': bookmaker, 'market': '90min_asian_handicap',
            'captured_at': captured_at, 'snapshot_at': _stamp(snapshot), 'source': 'infersports-' + bookmaker,
            'url': url, 'content_sha256': content_sha256, 'provider_event_id': event['id'],
            'provider_kickoff_at': event['scheduled_at'], 'market_status': 'provider_reports_open',
            'lines': [lines[line] for line in sorted(lines)], 'opening_lines': opening_lines,
            'line_selection': 'closest_price_balance_not_provider_main', 'market_1x2': market_1x2,
            'source_delay_seconds': 90, 'snapshot_age_seconds': age,
            'freshness_evidence': 'provider stale=false and fresh global feed health' if health_fresh else 'recent provider update',
            'provenance': 'Third-party bookmaker attribution; unchanged prices not independently polled at bookmaker'}


def attach_crown_ah(root, fixtures, now, fetch=None, *, probe=False):
    result = {'markets': {}, 'sources': [], 'problems': []}
    transport = fetch or _fetch
    if now.utcoffset() is None:
        raise ValueError('timezone required')
    eligible = [f for f in fixtures if f.get('provider') == 'espn' and f.get('league') == 'england-premier'
                and f.get('status') == 'scheduled' and f.get('home_id') and f.get('away_id')
                and f.get('kickoff_at') and _instant(f['kickoff_at']) > now]
    if not eligible and not probe:
        result['sources'].append({'name': 'infersports-crown-ah', 'status': 'not_requested', 'reason': 'no eligible fixtures'})
        return result
    aliases = _aliases(root)
    config_path = Path(root) / 'data/f4/config.json'
    config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    preferences = config.get('asian_handicap', {}).get('bookmaker_priority', ['crown'])
    budget_path = Path(root) / 'data/f4/ah/api-budget.json'
    day = now.astimezone(timezone.utc).date().isoformat()
    budget = json.loads(budget_path.read_text(encoding='utf-8')) if budget_path.exists() else {}
    if budget.get('day') != day:
        budget = {'day': day, 'requests': 0, 'limit': MAX_DAILY}

    def get(url):
        if budget.get('exhausted') or budget['requests'] >= MAX_DAILY:
            raise ValueError('daily source budget exhausted')
        metadata = {'name': 'infersports-crown-ah', 'url': url, 'status': 'error'}
        try:
            import requests
            for attempt in range(2):
                if budget['requests'] >= MAX_DAILY:
                    raise ValueError('daily source budget exhausted')
                budget['requests'] += 1
                try:
                    received = transport(url)
                    break
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                    if attempt:
                        raise
            payload, captured = received['payload'], received['captured_at']
            if not isinstance(payload, dict) or payload.get('error'):
                raise ValueError('malformed source response')
            content_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
            metadata.update(status='ok', captured_at=captured, content_sha256=content_hash)
            return payload, captured, content_hash
        except Exception as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status == 429:
                budget['exhausted'] = True
            metadata['error'] = type(exc).__name__
            metadata['http_status'] = status
            raise
        finally:
            budget_path.parent.mkdir(parents=True, exist_ok=True)
            budget_path.write_text(json.dumps(budget, indent=2) + '\n', encoding='utf-8')
            result['sources'].append(metadata)

    try:
        books, _, _ = get(BASE + '/bookmakers')
        verified = {b['key'] for b in books.get('data', []) if isinstance(b.get('key'), str)
                    and b.get('key', '').lower() == b.get('name', '').lower()}
        allowed = [name for name in preferences if name in verified]
        if not allowed:
            raise ValueError('allowed bookmaker mapping unverified')
        health, health_captured, _ = get('https://api.infersports.dev/health')
        if (health.get('feed_live') is not True or health.get('as_of_stale') is not False
                or not 0 <= (_instant(health_captured) - _instant(health['as_of'])).total_seconds() <= MAX_FEED_AGE_SECONDS):
            raise ValueError('provider feed health unavailable or stale')
        if not eligible:
            # A metadata probe verifies API access, not quote availability.
            result['sources'].append({'name': 'infersports-crown-ah-quotes', 'status': 'not_requested',
                                      'reason': 'no confirmed EPL fixture in capture window'})
            return result
        by_date = {}
        for fixture in eligible:
            date = _instant(fixture['kickoff_at']).date().isoformat()
            if date not in by_date:
                data, _, _ = get(BASE + '/events?' + urlencode({'sport': 'football', 'league': LEAGUE_ID,
                                                               'status': 'scheduled', 'date': date, 'limit': 100}))
                if data.get('page', {}).get('next_cursor') is not None:
                    raise ValueError('EPL event list unexpectedly truncated')
                by_date[date] = data.get('data', [])
            possible = [event for event in by_date[date] if _identity(event, fixture, aliases)]
            if len(possible) != 1:
                result['problems'].append(f"{fixture['match_id']}: unique Crown source fixture not found")
                continue
            event = possible[0]
            query = '?' + urlencode({'bookmakers': ','.join(allowed), 'markets': 'asian_handicap,1x2',
                                      'period': 'full_time', 'format': 'decimal'})
            url = BASE + '/events/' + event['id'] + '/odds' + query
            try:
                payload, captured, content_hash = get(url)
                opening = None
                opening_warning = None
                try:
                    opening, opened_captured, opening_hash = get(BASE + '/events/' + event['id'] + '/opening' + query)
                    opening = {**opening, '_captured_at': opened_captured, '_content_sha256': opening_hash}
                except Exception as exc:
                    opening_warning = 'Declared opening unavailable: ' + type(exc).__name__
                quote, errors = None, []
                for company in allowed:
                    try:
                        quote = parse_quotes(payload, event, fixture, aliases, captured, url, content_hash, opening,
                                             bookmaker=company, health=health)
                        break
                    except (ValueError, TypeError, KeyError) as exc:
                        errors.append(company + ': ' + str(exc))
                if quote is None:
                    raise ValueError('; '.join(errors))
                quote['bookmaker_priority'] = allowed
                quote['earlier_bookmaker_rejections'] = errors
                if opening_warning:
                    quote['opening_warning'] = opening_warning
                result['markets'][fixture['match_id']] = quote
            except Exception as exc:
                result['problems'].append(f"{fixture['match_id']}: Crown AH unavailable ({type(exc).__name__}: {str(exc)[:160]})")
    except Exception as exc:
        result['problems'].append(f'Crown AH source unavailable ({type(exc).__name__}: {str(exc)[:160]})')
    return result
