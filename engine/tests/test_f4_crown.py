"""Public Crown adapter contracts; synthetic documents only, no live requests."""
import json
from datetime import datetime, timezone

import pytest

import f4_crown as crown


NOW = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
CAPTURED = '2026-10-01T10:00:00Z'


@pytest.fixture
def root(tmp_path):
    path = tmp_path / 'data/01-teams/_aliases.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'england': {
        'arsenal': {'en': 'Arsenal', 'zh': '阿森纳'},
        'chelsea': {'en': 'Chelsea', 'zh': '切尔西'},
        'liverpool': {'en': 'Liverpool', 'zh': '利物浦'},
    }}), encoding='utf-8')
    return tmp_path


def schedule(*, home='阿森纳', away='切尔西', kickoff='26-10-01 21:00', eid='123'):
    cells = [''] * 13
    cells[1] = '<a href="/league?SclassID=36">英超</a>'
    cells[2] = kickoff
    cells[3] = '<font>[1]</font><a>' + home + '</a>'
    cells[11] = '<a>' + away + '</a><font>[8]</font>'
    cells[12] = '<a href="/Oddslist/' + eid + '.htm">详情</a>'
    return '<table id="table_schedule"><tr>' + ''.join('<td>' + x + '</td>' for x in cells) + '</tr></table>'


def fixture(**changes):
    row = {'match_id': 'espn:991', 'provider': 'espn', 'league': 'england-premier',
           'status': 'scheduled', 'home_id': 'arsenal', 'away_id': 'chelsea',
           'kickoff_at': '2026-10-01T13:00:00Z'}
    row.update(changes)
    return row


def quote_fields():
    fields = ['0'] * 24
    fields[0:3] = ['545', 'quote-22', 'Crown']
    fields[3:6] = ['1.60', '4.00', '6.00']  # Opening odds must never win.
    fields[10:13] = ['2.10', '3.40', '3.60']
    fields[20] = '2026,10-1,1,9,55,0'
    fields[21] = 'Crow*'
    return fields


def details(*, fields=None, rows=None, overrides=None):
    values = {'ScheduleID': 123, 'sclassId': 36, 'matchname_cn': '英超',
              'matchname': 'English Premier League', 'sclassKind': 1, 'ifShow': 1,
              'neutrality': '0', 'hometeam': 'Arsenal', 'hometeam_cn': '阿森纳',
              'guestteam': 'Chelsea', 'guestteam_cn': '切尔西',
              'hometeamID': 10, 'guestteamID': 20,
              'MatchTime': '2026,10-1,1,13,0,0'}
    values.update(overrides or {})
    statements = ['var ' + name + '=' + json.dumps(value, ensure_ascii=False) + ';' for name, value in values.items()]
    quote_rows = rows if rows is not None else ['|'.join(fields or quote_fields())]
    statements.append('var game=Array(' + ','.join(json.dumps(x) for x in quote_rows) + ');')
    return '\n'.join(statements)


def parse(root, *, text=None, match=None, captured=CAPTURED, index=None):
    aliases = crown._aliases(root)
    row = crown.parse_index(index or schedule(), aliases)[0]
    return crown.parse_quote(text or details(), row, match or fixture(), aliases,
                             captured, crown.DATA_BASE + '123.js', 'a' * 64)


def transport(*, body=None, company=None):
    responses = {
        crown.COMPANY_URL: company if company is not None else "<a href='company.aspx?id=545&company=Crow*'>Crow*</a>",
        crown.INDEX_URL: schedule(),
        crown.TOMORROW_URL: '<table id="table_schedule"></table>',
        crown.DATA_BASE + '123.js': body or details(),
    }
    calls = []
    def fetch(url):
        calls.append(url)
        return {'body': responses[url], 'captured_at': CAPTURED}
    fetch.calls = calls
    return fetch


def test_index_utc8_and_javascript_utc_agree(root):
    row = crown.parse_index(schedule(), crown._aliases(root))[0]
    assert row['kickoff_at'] == '2026-10-01T13:00:00Z'
    assert row['home_id'] == 'arsenal'
    assert row['away_id'] == 'chelsea'
    assert row['home'] == '阿森纳'
    assert crown.parse_js_time('2026,10-1,1,13,0,0') == datetime(2026, 10, 1, 13, tzinfo=timezone.utc)


def test_latest_odds_and_crown_identity_provenance_only(root):
    quote = parse(root)
    assert quote['odds'] == [2.1, 3.4, 3.6]
    assert quote['bookmaker'] == 'crown'
    assert quote['company_id'] == 545
    assert quote['market'] == '90min_1x2'
    assert quote['published_at'] == '2026-10-01T09:55:00Z'
    assert quote['captured_at'] == CAPTURED
    assert quote['content_sha256'] == 'a' * 64
    assert 'raw' not in quote and 'body' not in quote


@pytest.mark.parametrize(('position', 'bad'), [(0, '546'), (2, 'Other'), (21, 'Other*'), (23, '1')])
def test_company_numeric_id_and_both_labels_required(root, position, bad):
    fields = quote_fields()
    fields[position] = bad
    with pytest.raises(ValueError, match='Crown'):
        parse(root, text=details(fields=fields))


@pytest.mark.parametrize('rows', [[], ['missing'], ['|'.join(quote_fields())] * 2])
def test_missing_or_duplicate_crown_is_rejected(root, rows):
    with pytest.raises(ValueError, match='Crown'):
        parse(root, text=details(rows=rows))


@pytest.mark.parametrize('bad', ['NaN', 'Infinity', '1', '0', '-2'])
def test_invalid_latest_odds_never_fall_back_to_opening(root, bad):
    fields = quote_fields()
    fields[10] = bad
    with pytest.raises(ValueError, match='latest Crown odds'):
        parse(root, text=details(fields=fields))


@pytest.mark.parametrize('captured', ['2026-10-01T13:00:00Z', '2026-10-01T13:01:00Z'])
def test_quote_at_or_after_kickoff_rejected(root, captured):
    with pytest.raises(ValueError, match='at or after kickoff'):
        parse(root, captured=captured)


def test_future_update_rejected(root):
    fields = quote_fields()
    fields[20] = '2026,10-1,1,10,1,0'
    with pytest.raises(ValueError, match='future'):
        parse(root, text=details(fields=fields))


@pytest.mark.parametrize('overrides', [
    {'ScheduleID': 124}, {'sclassId': 37},
    {'hometeam': 'Liverpool', 'hometeam_cn': '利物浦'},
    {'guestteam': 'Arsenal', 'guestteam_cn': '阿森纳'},
    {'MatchTime': '2026,10-1,1,13,6,0'},
])
def test_detail_identity_and_kickoff_must_match_fixture(root, overrides):
    with pytest.raises(ValueError, match='conflict'):
        parse(root, text=details(overrides=overrides))


def test_no_due_matches_only_probes_health_without_detail(root):
    fetch = transport()
    result = crown.attach_crown(root, [fixture(kickoff_at='2026-10-02T13:00:00Z')], NOW, fetch=fetch)
    assert not result['markets'] and not result['problems']
    assert fetch.calls == [crown.COMPANY_URL, crown.INDEX_URL]
    assert result['sources'][-1]['status'] == 'not_requested'


def test_attach_returns_only_verified_crown_quote_without_raw(root):
    fetch = transport()
    result = crown.attach_crown(root, [fixture()], NOW, fetch=fetch)
    assert not result['problems']
    assert list(result['markets']) == ['espn:991']
    assert result['markets']['espn:991']['bookmaker'] == 'crown'
    assert result['markets']['espn:991']['odds'] == [2.1, 3.4, 3.6]
    assert fetch.calls == [crown.COMPANY_URL, crown.INDEX_URL, crown.TOMORROW_URL, crown.DATA_BASE + '123.js']
    assert all(source['status'] == 'ok' and len(source['content_sha256']) == 64 for source in result['sources'])
    serialized = json.dumps(result)
    assert 'var game' not in serialized and '<table' not in serialized
    assert '"body"' not in serialized and '"raw"' not in serialized


def test_public_company_mapping_is_required_before_any_quote(root):
    fetch = transport(company="<a href='company.aspx?id=546&company=Crow*'>Crow*</a>")
    result = crown.attach_crown(root, [fixture()], NOW, fetch=fetch)
    assert not result['markets']
    assert result['problems']
    assert fetch.calls == [crown.COMPANY_URL]


def test_wrong_sides_on_index_do_not_trigger_a_detail_request(root):
    original = transport()
    calls = []
    def fetch(url):
        calls.append(url)
        if url == crown.INDEX_URL:
            return {'body': schedule(home='切尔西', away='阿森纳'), 'captured_at': CAPTURED}
        return original(url)
    result = crown.attach_crown(root, [fixture()], NOW, fetch=fetch)
    assert not result['markets']
    assert crown.DATA_BASE + '123.js' not in calls
    assert result['problems']
