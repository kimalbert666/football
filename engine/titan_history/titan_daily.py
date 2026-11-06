"""Daily, bounded research archive of source-reported openings and history.

Reuses independent ESPN fixtures, checks provider mapping, preserves all failure
states. Research records do not enter the prospective prediction/training ledger.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import requests

from titan_cloud_probe import Document, Node, clean, direct_cells
from titan_browser_probe import restriction_reason
from titan_batch_reader import browser_run, validate_targets, stamp, RECONCILED_STATUSES
from titan_fixture_mapping import map_fixture_candidates

HK = timezone(timedelta(hours=8))


def visible_text(node):
    if not node.visible() or node.tag in {'script', 'style'} or node.attrs.get('name') == 'order':
        return ''
    return ' '.join(visible_text(c) if isinstance(c, Node) else c for c in node.children)


def parse_schedule(html, day, aliases, resolve):
    """Only explicit EPL rows and event IDs from the public dated schedule."""
    doc = Document(html)
    result = []
    for row in doc.root.descendants('tr'):
        cells = direct_cells(row)
        if len(cells) < 10 or clean(visible_text(cells[0])) != '英超':
            continue
        event = row.attrs.get('sid', '')
        league = row.attrs.get('name', '').split(',')[0]
        linked_ids = set()
        for link in cells[-1].descendants('a'):
            match = re.fullmatch(r'AsianOdds\((\d+)\)', link.attrs.get('onclick', '').strip())
            if match:
                linked_ids.add(match[1])
        if not re.fullmatch(r'\d+', event) or linked_ids != {event} or league != '36':
            raise ValueError('EPL schedule event or league identity conflict')
        shown_time = clean(visible_text(cells[1]))
        parts = re.fullmatch(r'(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})', shown_time)
        if not parts:
            raise ValueError('schedule kickoff display invalid')
        month, number, hour, minute = map(int, parts.groups())
        # Actual dated pages include the following early morning (e.g.
        # Next_20261031 has Nov 1 01:30 and 04:00). Keep the displayed date;
        # do not force the URL date onto the fixture, including at New Year.
        choices = [d for d in (day, day+timedelta(days=1)) if (d.month, d.day)==(month, number)]
        if len(choices) != 1:
            raise ValueError('dated schedule row day mismatch')
        kickoff = datetime.combine(choices[0], datetime.min.time()).replace(hour=hour, minute=minute)
        home, away = clean(visible_text(cells[3])), clean(visible_text(cells[5]))
        result.append({'provider_event_id': event, 'league_id': league,
                       'home': home, 'away': away, 'home_id': resolve([home], aliases),
                       'away_id': resolve([away], aliases),
                       'kickoff_at': kickoff.replace(tzinfo=HK).astimezone(timezone.utc).isoformat()})
    return result


def read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def write_json(path, value, immutable=False):
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        if path.read_text(encoding='utf-8') != content:
            raise ValueError('immutable research archive conflict')
        return
    path.write_text(content, encoding='utf-8')


def run(root, max_matches=10):
    sys.path.insert(0, str(root/'engine/scripts'))
    from f4_sources import _aliases, _resolve, _instant
    now = datetime.now(timezone.utc)
    dest = root/'data/f4/titan-history'
    fixtures = []
    for path in (root/'data/f4/fixtures').glob('*.json'):
        f = read_json(path)
        if f.get('league') != 'england-premier' or not f.get('kickoff_at'):
            continue
        if now-timedelta(days=7) <= _instant(f['kickoff_at']) <= now+timedelta(days=7):
            fixtures.append(f)
    aliases = _aliases(root)
    dates = sorted({d for f in fixtures
                    for d in (_instant(f['kickoff_at']).astimezone(HK).date(),
                              _instant(f['kickoff_at']).astimezone(HK).date()-timedelta(days=1))})
    candidates, sources, blocked = [], [], False
    for day in dates:
        url = f'https://bf.titan007.com/football/Next_{day:%Y%m%d}.htm'
        meta = {'url': url, 'checked_at_utc': stamp(), 'status': 'not_attempted'}
        sources.append(meta)
        if blocked:
            meta['status'] = 'skipped_after_source_restriction'
            continue
        try:
            response = requests.get(url, timeout=15, allow_redirects=False)
            meta['http_status'] = response.status_code
            text = response.content.decode('gb18030', errors='strict') if response.status_code == 200 else ''
            reason = restriction_reason(response.status_code, {'body': text[:25000]})
            if reason:
                meta.update(status='restricted', reason=reason)
                blocked = True
                continue
            if response.status_code != 200:
                meta['status'] = 'http_error'
                continue
            rows = parse_schedule(text, day, aliases, _resolve)
            meta.update(status='ok', captured_at_utc=stamp(), candidate_count=len(rows),
                        content_sha256=hashlib.sha256(response.content).hexdigest())
            for row in rows:
                row['mapping_source'] = meta.copy()
            candidates.extend(rows)
        except Exception as exc:
            meta.update(status='read_or_parse_error', error_type=type(exc).__name__)
            if isinstance(exc, ValueError):
                meta['parse_error'] = str(exc)[:160]
    mapping = map_fixture_candidates(fixtures, candidates, titan_league_id='36')
    status = {'schema': 'f4-titan-history-daily-v1', 'started_at_utc': now.isoformat(),
              'scope': 'archived independent EPL fixtures within past/future seven days',
              'fixture_source': 'existing ESPN fixture archive', 'sources': sources,
              'mapping': mapping, 'formal_observation': False, 'training_eligible': False,
              'bookmaker_first_opening_certified': False, 'targets': [], 'results': []}
    write_json(dest/'status.json', status)
    by_id = {f['match_id']: f for f in fixtures}
    due = []
    for m in mapping['coverage']:
        if m['state'] != 'mapped_candidate' or blocked:
            continue
        f = by_id[m['match_id']]
        event = m['candidate']['provider_event_id']
        previous = read_json(dest/'events'/f'{event}.json', {})
        # An actual post-match read can be retained; no endless rereading of a
        # completed fixture after its displayed opening/history reconciles.
        if (previous.get('status') in RECONCILED_STATUSES
                and _instant(previous['captured_at_utc']) >= _instant(f['kickoff_at'])+timedelta(hours=3)):
            m['history_state'] = 'archived_after_match'
            continue
        if previous and now-_instant(previous['captured_at_utc']) < timedelta(hours=20):
            m['history_state'] = 'await_next_daily_refresh'
            continue
        last = previous.get('captured_at_utc', '0001-01-01T00:00:00Z')
        # Matches starting soon first, then least recently attempted; incomplete
        # older reads cannot permanently monopolize the daily quota.
        soon = now <= _instant(f['kickoff_at']) <= now+timedelta(days=2)
        due.append((not soon, last, f['kickoff_at'], m))
    for _, _, _, m in sorted(due, key=lambda item: item[:3])[:max_matches]:
        c = m['candidate']; f = by_id[m['match_id']]
        evidence = next(r['mapping_source'] for r in candidates if r['provider_event_id'] == c['provider_event_id'])
        status['targets'].append({'provider_event_id': c['provider_event_id'], 'home': c['home'],
            'away': c['away'], 'kickoff_display': _instant(f['kickoff_at']).astimezone(HK).strftime('%Y-%m-%d %H:%M'),
            'company_ids': ['3'], 'canonical_match_id': m['match_id'],
            'home_id': f['home_id'], 'away_id': f['away_id'],
            'independent_fixture_kickoff_at': f['kickoff_at'],
            'mapping_source_evidence': evidence,
            'independent_fixture_last_seen_at': f.get('last_seen_at'),
            'mapping_basis': 'strict canonical sides and kickoff match to ESPN; odds timezone unverified'})
    batch = {'fixtures': []}
    if status['targets']:
        validate_targets(status['targets'])
        try:
            asyncio.run(asyncio.wait_for(browser_run(status['targets'], batch), 480))
        except Exception as exc:
            status['batch_error_type'] = type(exc).__name__
    for entry in batch['fixtures']:
        event = entry['fixture']['provider_event_id']
        captured = stamp()
        payload = {'captured_at_utc': captured, 'run_id': __import__('os').environ.get('GITHUB_RUN_ID'), **entry}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]
        path = dest/'observations'/event/(now.strftime('%Y%m%dT%H%M%S')+'-'+digest+'.json')
        write_json(path, payload, immutable=True)
        brief = {'provider_event_id': event, 'match_id': entry['fixture']['canonical_match_id'],
                 'captured_at_utc': captured, 'status': entry['status'], 'archive_path': path.relative_to(root).as_posix(),
                 'company_history_counts': {k: h['prematch_quote_count'] for k, h in entry.get('histories', {}).items()}}
        write_json(dest/'events'/f'{event}.json', brief)
        status['results'].append(brief)
    attempted = {r['provider_event_id'] for r in status['results']}
    for target in status['targets']:
        if target['provider_event_id'] not in attempted:
            status['results'].append({'provider_event_id': target['provider_event_id'], 'status': 'not_attempted_budget_or_runtime_error'})
    status.update(finished_at_utc=stamp(), planned=len(status['targets']),
                  reconciled=sum(r['status'] in RECONCILED_STATUSES for r in status['results']),
                  deferred_by_daily_budget=max(0, len(due)-max_matches))
    write_json(dest/'status.json', status)
    print(json.dumps({k: status[k] for k in ('planned','reconciled','deferred_by_daily_budget')}, ensure_ascii=False))
    return status


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    options = parser.parse_args()
    run(options.root.resolve())
