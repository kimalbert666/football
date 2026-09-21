"""Contract tests for fresh calendar, identity joins, and regulation-time results."""
import copy
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

import f4_sources as sources


@pytest.fixture
def root(tmp_path):
    path = tmp_path / 'data/01-teams/_aliases.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'england': {
        'arsenal': {'espn': 'Arsenal', 'zh': '阿森纳', 'variants': ['The Arsenal']},
        'chelsea': {'espn': 'Chelsea', 'zh': '切尔西'},
        'liverpool': {'espn': 'Liverpool', 'zh': '利物浦'},
    }}), encoding='utf-8')
    return tmp_path


@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def event(now, eid='101', *, status='STATUS_SCHEDULED', completed=False, home='Arsenal', away='Chelsea'):
    date = sources._stamp(now + timedelta(hours=2))
    return {'id': eid, 'date': date, 'competitions': [{
        'id': eid, 'date': date, 'timeValid': True,
        'status': {'type': {'name': status, 'completed': completed,
                            'state': 'post' if completed else 'pre'}},
        'competitors': [
            {'id': '10', 'homeAway': 'home', 'team': {'id': '10', 'displayName': home}, 'score': '2'},
            {'id': '20', 'homeAway': 'away', 'team': {'id': '20', 'displayName': away}, 'score': '1'}]}]}


def quote(now, mid=900, **changes):
    kickoff = (now + timedelta(hours=2)).astimezone(sources.CHINA)
    changed = (now - timedelta(days=3)).astimezone(sources.CHINA)
    row = {'matchId': mid, 'leagueAbbName': '英超', 'homeTeamAbbName': '阿森纳', 'awayTeamAbbName': '切尔西',
           'matchDate': kickoff.strftime('%Y-%m-%d'), 'matchTime': kickoff.strftime('%H:%M:%S'),
           'matchStatus': 'Selling', 'sellStatus': 1,
           'poolList': [{'poolCode': 'HAD', 'poolStatus': 'Selling'}],
           'had': {'h': '2.10', 'd': '3.20', 'a': '3.50',
                   'updateDate': changed.strftime('%Y-%m-%d'), 'updateTime': changed.strftime('%H:%M:%S')}}
    row.update(changes)
    return row


def transport(now, events=(), quotes=(), *, fail_days=(), summaries=None, malformed=None):
    calls = []
    def fetch(url):
        calls.append(url)
        parsed = urlparse(url)
        if 'scoreboard' in url:
            day = parse_qs(parsed.query)['dates'][0]
            if day in fail_days:
                raise OSError('offline')
            if malformed is not None:
                return copy.deepcopy(malformed)
            return {'leagues': [{'slug': 'eng.1'}],
                    'events': copy.deepcopy(list(events)) if day == now.strftime('%Y%m%d') else []}
        if 'summary' in url:
            return copy.deepcopy((summaries or {})[parse_qs(parsed.query)['event'][0]])
        return {'success': True, 'value': {'matchInfoList': [{'subMatchList': copy.deepcopy(list(quotes))}],
                                          'lastUpdateTime': 'response-metadata-only'}}
    fetch.calls = calls
    return fetch


def test_calendar_independent_of_market_and_real_capture_clock(root, now):
    fetch = transport(now, [event(now)])
    before = datetime.now(timezone.utc)
    result = sources.collect(root, now - timedelta(seconds=30), include_market=False, fetch=fetch)
    after = datetime.now(timezone.utc)
    assert result['coverage']['complete']
    assert len(fetch.calls) == 2
    assert all(len(parse_qs(urlparse(url).query)['dates'][0]) == 8 for url in fetch.calls)
    assert result['fixtures'][0]['market'] is None
    assert before <= sources._instant(result['captured_at']) <= after
    assert all(set(s) == {'name', 'status', 'url', 'captured_at', 'content_sha256'} for s in result['sources'])
    assert all(len(s['content_sha256']) == 64 for s in result['sources'])
    assert not (root / 'engine/cache').exists()


def test_unique_exact_quote_join_keeps_old_last_change_date(root, now):
    fetch = transport(now, [event(now)], [quote(now)])
    result = sources.collect(root, now, fetch=fetch)
    row = result['fixtures'][0]
    assert row['match_id'] == 'espn:101'
    assert row['home_id'] == 'arsenal'
    assert row['market']['odds'] == [2.1, 3.2, 3.5]
    assert row['market']['provider_event_id'] == '900'
    assert sources._instant(row['market']['published_at']) < now - timedelta(days=2)
    assert sources._instant(row['market']['captured_at']) <= sources._instant(result['captured_at'])
    assert row['score'] is None


@pytest.mark.parametrize(('status', 'completed', 'expected'), [
    ('STATUS_FULL_TIME', True, 'completed'), ('STATUS_CANCELED', True, 'cancelled'),
    ('STATUS_POSTPONED', False, 'postponed'), ('STATUS_FINAL_AET', True, 'unknown'),
    ('STATUS_FINAL_PEN', True, 'unknown'), ('STATUS_FINAL', True, 'unknown'),
])
def test_only_verified_regulation_result_is_scored(root, now, status, completed, expected):
    item = event(now, status=status, completed=completed)
    result = sources.collect(root, now, False, fetch=transport(now, [item]))
    row = result['fixtures'][0]
    assert row['status'] == expected
    assert row['score'] == ([2, 1] if expected == 'completed' else None)


@pytest.mark.parametrize('broken', ['time_false', 'time_missing', 'missing_date', 'comp_id', 'team_id', 'alias'])
def test_ambiguous_rows_retained_and_coverage_unhealthy(root, now, broken):
    item = event(now)
    comp = item['competitions'][0]
    if broken == 'time_false':
        comp['timeValid'] = False
    elif broken == 'time_missing':
        del comp['timeValid']
    elif broken == 'missing_date':
        item.pop('date')
        comp.pop('date')
    elif broken == 'comp_id':
        comp['id'] = '909'
    elif broken == 'team_id':
        comp['competitors'][0]['team']['id'] = '909'
    else:
        comp['competitors'][0]['team']['displayName'] = 'Arsenal Academy'
    result = sources.collect(root, now, False, fetch=transport(now, [item]))
    assert len(result['fixtures']) == 1
    assert result['fixtures'][0]['problems']
    assert not result['coverage']['complete']
    assert result['fixtures'][0]['score'] is None


def test_variants_resolve_but_ambiguous_aliases_do_not(root, now):
    path = root / 'data/01-teams/_aliases.json'
    item = event(now, home='The Arsenal')
    result = sources.collect(root, now, False, fetch=transport(now, [item]))
    assert result['fixtures'][0]['home_id'] == 'arsenal'
    aliases = json.loads(path.read_text())
    aliases['england']['chelsea']['variants'] = ['The Arsenal']
    path.write_text(json.dumps(aliases), encoding='utf-8')
    result = sources.collect(root, now, False, fetch=transport(now, [item]))
    assert result['fixtures'][0]['home_id'] is None
    assert not result['coverage']['complete']


def test_duplicate_event_identity_conflict_disables_result(root, now):
    result = sources.collect(root, now, False, fetch=transport(now, [event(now), event(now, away='Liverpool')]))
    assert len(result['fixtures']) == 1
    assert result['fixtures'][0]['home_id'] is None
    assert result['fixtures'][0]['status'] == 'unknown'
    assert not result['coverage']['complete']


def test_partial_calendar_fallback_never_claims_full_coverage(root, now):
    fetch = transport(now, quotes=[quote(now)], fail_days=[now.strftime('%Y%m%d')])
    result = sources.collect(root, now, fetch=fetch)
    assert not result['coverage']['complete']
    row = result['fixtures'][0]
    assert row['match_id'] == 'sporttery:900'
    assert row['status'] == 'unknown'
    assert row['score'] is None
    assert result['sources'][0]['status'] == 'error'


@pytest.mark.parametrize('malformed', [{}, {'events': []}, {'leagues': [{'slug': 'eng.1'}], 'events': 'wrong'},
                                     {'leagues': [{'slug': 'eng.1'}], 'events': [None]}])
def test_malformed_calendar_is_explicit_source_failure(root, now, malformed):
    result = sources.collect(root, now, False, fetch=transport(now, malformed=malformed))
    assert not result['coverage']['complete']
    assert result['problems']
    assert all(s['status'] == 'error' for s in result['sources'])


@pytest.mark.parametrize('broken', ['closed', 'nonselling', 'nan', 'inf', 'bool', 'past', 'future_change'])
def test_unusable_quotes_cannot_reach_market(root, now, broken):
    item = quote(now)
    if broken == 'closed':
        item['poolList'][0]['poolStatus'] = 'Closed'
    elif broken == 'nonselling':
        item['sellStatus'] = 0
    elif broken in ('nan', 'inf', 'bool'):
        item['had']['h'] = True if broken == 'bool' else broken
    elif broken == 'past':
        date = (now - timedelta(hours=1)).astimezone(sources.CHINA)
        item.update(matchDate=date.strftime('%Y-%m-%d'), matchTime=date.strftime('%H:%M:%S'))
    else:
        date = (now + timedelta(hours=1)).astimezone(sources.CHINA)
        item['had'].update(updateDate=date.strftime('%Y-%m-%d'), updateTime=date.strftime('%H:%M:%S'))
    result = sources.collect(root, now, fetch=transport(now, [event(now)], [item]))
    assert result['fixtures'][0]['market'] is None
    assert result['problems']


@pytest.mark.parametrize('ambiguous', ['duplicate_quotes', 'duplicate_fixtures', 'reversed', 'late'])
def test_quote_mapping_requires_unique_sides_and_five_minute_time(root, now, ambiguous):
    items, quotes = [event(now)], [quote(now)]
    if ambiguous == 'duplicate_quotes':
        quotes.append(quote(now, mid=901))
    elif ambiguous == 'duplicate_fixtures':
        items.append(event(now, eid='102'))
    elif ambiguous == 'reversed':
        quotes[0].update(homeTeamAbbName='切尔西', awayTeamAbbName='阿森纳')
    else:
        date = (now + timedelta(hours=2, minutes=6)).astimezone(sources.CHINA)
        quotes[0].update(matchDate=date.strftime('%Y-%m-%d'), matchTime=date.strftime('%H:%M:%S'))
    result = sources.collect(root, now, fetch=transport(now, items, quotes))
    assert all(row['market'] is None for row in result['fixtures'])
    assert result['problems']


def test_pending_uses_summary_and_verifies_header_identity(root, now):
    old = now - timedelta(days=4)
    item = event(old, status='STATUS_FULL_TIME', completed=True)
    item['league'] = {'slug': 'eng.1'}
    item['timeValid'] = item['competitions'][0].pop('timeValid')
    record = {'match_id': 'espn:101', 'source_event_id': '101', 'provider': 'espn', 'league': sources.LEAGUE,
              'kickoff_at': item['date'], 'home_id': 'arsenal', 'away_id': 'chelsea'}
    fetch = transport(now, summaries={'101': {'header': item}})
    result = sources.collect(root, now, False, pending=[record], fetch=fetch)
    assert result['fixtures'][0]['score'] == [2, 1]
    assert any('summary?event=101' in url for url in fetch.calls)
    item['id'] = '102'
    result = sources.collect(root, now, False, pending=[record], fetch=transport(now, summaries={'101': {'header': item}}))
    assert result['fixtures'][0]['score'] is None
    assert not result['coverage']['complete']


def test_pending_cap_and_daily_window(root, now):
    pending, summaries = [], {}
    for i in range(21):
        eid = str(100 + i)
        item = event(now - timedelta(days=5), eid=eid, status='STATUS_FULL_TIME', completed=True)
        item['league'] = {'slug': 'eng.1'}
        summaries[eid] = {'header': item}
        pending.append({'match_id': f'espn:{eid}', 'source_event_id': eid, 'provider': 'espn', 'league': sources.LEAGUE,
                        'kickoff_at': item['date'], 'home_id': 'arsenal', 'away_id': 'chelsea'})
    fetch = transport(now, summaries=summaries)
    result = sources.collect(root, now, False, pending=pending, fetch=fetch, lookahead_days=7, lookback_days=1)
    assert sum('scoreboard' in url for url in fetch.calls) == 9
    assert sum('summary' in url for url in fetch.calls) == 20
    assert not result['coverage']['complete']
    assert any('truncated' in p for p in result['problems'])


def test_market_failure_does_not_erase_calendar_coverage(root, now):
    original = transport(now, [event(now)])
    def fetch(url):
        return {'success': False, 'errorMessage': 'must not persist raw message'} if 'sporttery' in url else original(url)
    result = sources.collect(root, now, fetch=fetch)
    assert result['coverage']['complete']
    assert result['sources'][-1]['status'] == 'error'
    assert 'must not persist' not in json.dumps(result)


def http_response(status, payload=None):
    import requests
    response = requests.Response()
    response.status_code = status
    response.url = sources.SPORTTERY_URL
    response.headers['X-Private'] = 'private-header-marker'
    response._content = json.dumps(payload or {'private': 'private-body-marker'}).encode()
    return response


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504, 567])
def test_http_transient_retries_then_succeeds(monkeypatch, status):
    import requests
    from sporttery_fetch import HEADERS
    responses = [http_response(status), http_response(200, {'success': True})]
    calls, waits = [], []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)
    monkeypatch.setattr(requests, 'get', get)
    monkeypatch.setattr(sources.time, 'sleep', waits.append)
    assert sources._fetch(sources.SPORTTERY_URL) == {'success': True}
    assert len(calls) == 2
    assert calls[0][1]['headers'] == HEADERS
    assert waits == [1]


@pytest.mark.parametrize('status', [401, 403])
def test_http_auth_refusal_is_not_retried_and_status_is_preserved(root, now, monkeypatch, status):
    import requests
    calls, waits = [], []
    original = transport(now, [event(now)])
    def get(url, **kwargs):
        calls.append(url)
        return http_response(status)
    def fetch(url):
        return sources._fetch(url) if 'sporttery' in url else original(url)
    monkeypatch.setattr(requests, 'get', get)
    monkeypatch.setattr(sources.time, 'sleep', waits.append)
    result = sources.collect(root, now, fetch=fetch)
    assert len(calls) == 1
    assert waits == []
    assert result['sources'][-1]['http_status'] == status
    assert any(f'HTTP {status}' in problem for problem in result['problems'])
    assert 'private-body-marker' not in json.dumps(result)
    assert 'private-header-marker' not in json.dumps(result)
    assert result['coverage']['complete']


@pytest.mark.parametrize('failure', ['http', 'connection', 'timeout'])
def test_transport_retries_are_bounded_and_exhaustion_keeps_safe_metadata(root, now, monkeypatch, failure):
    import requests
    calls, waits = [], []
    original = transport(now, [event(now)])
    def get(url, **kwargs):
        calls.append(url)
        if failure == 'connection':
            raise requests.exceptions.ConnectionError('private-body-marker')
        if failure == 'timeout':
            raise requests.exceptions.Timeout('private-header-marker')
        return http_response(567)
    def fetch(url):
        return sources._fetch(url) if 'sporttery' in url else original(url)
    monkeypatch.setattr(requests, 'get', get)
    monkeypatch.setattr(sources.time, 'sleep', waits.append)
    result = sources.collect(root, now, fetch=fetch)
    assert len(calls) == 3
    assert waits == [1, 2]
    assert result['sources'][-1]['status'] == 'error'
    if failure == 'http':
        assert result['sources'][-1]['http_status'] == 567
        assert any('HTTP 567' in problem for problem in result['problems'])
    else:
        assert 'http_status' not in result['sources'][-1]
    assert 'private-body-marker' not in json.dumps(result)
    assert 'private-header-marker' not in json.dumps(result)


def test_crown_mode_never_requests_sporttery(root, now, monkeypatch):
    from unittest.mock import Mock
    config = root / 'data/f4/config.json'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({'odds_source': 'crown'}), encoding='utf-8')
    item = event(now + timedelta(hours=1))
    market = {'bookmaker': 'crown', 'odds': [2.0, 3.2, 4.0]}
    attach = Mock(return_value={'markets': {'espn:101': market}, 'sources': [], 'problems': []})
    monkeypatch.setattr('f4_crown.attach_crown', attach)
    fetch = transport(now, [item])
    result = sources.collect(root, now, fetch=fetch)
    assert all('sporttery' not in url for url in fetch.calls)
    assert len(attach.call_args.args[1]) == 1
    assert result['fixtures'][0]['market'] == market


def test_crown_daily_settlement_does_not_fetch_any_odds(root, now, monkeypatch):
    from unittest.mock import Mock
    config = root / 'data/f4/config.json'
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({'odds_source': 'crown'}), encoding='utf-8')
    attach = Mock(side_effect=AssertionError('daily must not request quotes'))
    monkeypatch.setattr('f4_crown.attach_crown', attach)
    fetch = transport(now, [event(now)])
    sources.collect(root, now, False, fetch=fetch)
    attach.assert_not_called()
    assert all('sporttery' not in url for url in fetch.calls)
