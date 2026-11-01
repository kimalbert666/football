"""Bounded ordinary-browser reader for explicitly identified Titan fixtures.

Research archive only. No credentials, stealth, proxy rotation, live prices,
automatic model fitting, notifications, or mutation of production observations.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit

from titan_browser_probe import DOM_PROJECTION, projected_html, restriction_reason
from titan_general import parse_opening_summary, validate_history_identity
from titan_history_archive import archive_history

PROJECTION = (DOM_PROJECTION.replace('(scope) => {', '({scope, home, away}) => {')
              .replace("labels[2] === '考文垂'", 'labels[2] === home')
              .replace("labels[4] === '纽卡斯尔联'", 'labels[4] === away')
              .replace("c.tagName === 'TD').map(cell", "(c.tagName === 'TD' || c.tagName === 'TH')).map(cell"))


def stamp():
    return datetime.now(timezone.utc).isoformat()


def same_destination(expected, actual):
    try:
        e, a = urlsplit(expected), urlsplit(actual)
        return (a.scheme == e.scheme == 'https' and a.hostname == e.hostname == 'vip.titan007.com'
                and a.port in (None, 443) and not a.username and not a.password and not a.fragment
                and a.path.lower() == e.path.lower()
                and sorted((k.lower(), v) for k, v in parse_qsl(a.query, keep_blank_values=True))
                == sorted((k.lower(), v) for k, v in parse_qsl(e.query, keep_blank_values=True)))
    except (ValueError, TypeError):
        return False


def validate_targets(targets):
    if not isinstance(targets, list) or not 1 <= len(targets) <= 10:
        raise ValueError('one to ten explicit fixtures required')
    seen = set()
    for item in targets:
        event = item.get('provider_event_id')
        if not isinstance(event, str) or not re.fullmatch(r'\d{1,12}', event) or event in seen:
            raise ValueError('unique numeric provider event required')
        seen.add(event)
        if not all(isinstance(item.get(k), str) and item[k].strip() for k in ('home', 'away', 'kickoff_display')):
            raise ValueError('fixture display identity required')
        if item['home'] == item['away']:
            raise ValueError('distinct home and away required')
        datetime.strptime(item['kickoff_display'], '%Y-%m-%d %H:%M')
        companies = item.get('company_ids', ['3'])
        if not isinstance(companies, list) or not 1 <= len(companies) <= 3 or len(companies) != len(set(companies)):
            raise ValueError('one to three unique company IDs required')
        if any(not isinstance(c, str) or not re.fullmatch(r'\d{1,6}', c) for c in companies):
            raise ValueError('numeric company ID required')


async def read_page(context, url, scope, fixture):
    page = await context.new_page()
    meta = {'url': url, 'checked_at_utc': stamp(), 'transport_ok': False,
            'transport': 'default_playwright_chromium', 'navigation_attempts': 1}
    try:
        response = await page.goto(url, wait_until='domcontentloaded', timeout=40000)
        status = response.status if response else None
        meta.update(http_status=status, final_url=page.url)
        denial = restriction_reason(status, {})
        if denial:
            meta['stop_reason'] = denial
            return meta, None
        if not same_destination(url, page.url):
            meta['error'] = 'unexpected_navigation_destination'
            return meta, None
        projection = await page.evaluate(PROJECTION, {'scope': scope, 'home': fixture['home'], 'away': fixture['away']})
        meta.update(captured_at_utc=stamp(), page_title=projection.get('title'),
                    visible_row_count=projection.get('row_count', 0))
        denial = restriction_reason(status, projection)
        if denial:
            meta['stop_reason'] = denial
            return meta, None
        if status != 200 or not projection.get('table_found'):
            meta['error'] = 'expected_quote_table_unavailable'
            return meta, None
        meta['transport_ok'] = True
        meta['projection_truncated'] = projection.get('row_count', 0) > 2000
        return meta, projection
    finally:
        await page.close()


def prepare_archive(projection, fixture, company, meta):
    headers = [[c.get('text', '').strip() for c in row] for row in projection['rows']
               if len(row) == 7 and all(c.get('visible') for c in row)
               and row[0].get('text', '').strip() == '时间'
               and row[1].get('text', '').strip() == '比分']
    identity = validate_history_identity(
        {'header_labels': headers[0] if len(headers) == 1 else [],
         'table_found': projection.get('table_found'), 'table_count': 1,
         'projection_truncated': meta.get('projection_truncated', False)},
        meta['final_url'], fixture['provider_event_id'], company['company_id'], fixture['home'], fixture['away'])
    kept = [(i, row) for i, row in enumerate(projection['rows'])
            if row and row[-1].get('text', '').strip() not in {'滚', '滾'}]
    archive = archive_history(
        projected_html({**projection, 'rows': [r for _, r in kept]}),
        kickoff_display=fixture['kickoff_display'], captured_at_utc=meta['captured_at_utc'],
        source_url=meta['final_url'], opening_display=company['opening']['displayed_full_datetime'],
        projection_truncated=meta.get('projection_truncated', False),
        fixture_identity_verified=identity['identity_verified'], odds_format_verified=False)
    first, opening = archive['earliest_visible_quote'], company['opening']
    quote = first.get('quote') or {}
    equal = (first.get('state') == 'unique_at_displayed_minute'
             and first.get('displayed_datetime') == opening['displayed_full_datetime']
             and quote.get('home_handicap') == opening['home_handicap']
             and quote.get('home_water_raw') is not None
             and float(quote['home_water_raw']) == float(opening['home_water_raw'])
             and float(quote['away_water_raw']) == float(opening['away_water_raw']))
    archive.update(provider_event_id=fixture['provider_event_id'], company_id=company['company_id'],
                   company_label=company['company_label'], identity_check=identity,
                   source_table_row_indices=[i for i, _ in kept],
                   source_reported_opening=opening, provider_opening_reconciled=bool(equal),
                   excluded_live_rows=len(projection['rows']) - len(kept),
                   source_fixture_identity=fixture,
                   independent_fixture_mapping_verified=False)
    return archive


async def collect(context, targets, output):
    stopped = False
    for fixture in targets:
        entry = {'fixture': fixture, 'status': 'not_attempted', 'histories': {}, 'pages': []}
        output['fixtures'].append(entry)
        if stopped:
            entry['status'] = 'skipped_after_source_restriction'
            continue
        event = fixture['provider_event_id']
        try:
            meta, projection = await asyncio.wait_for(read_page(context,
                f'https://vip.titan007.com/AsianOdds_n.aspx?id={event}', 'summary', fixture), 45)
            entry['pages'].append(meta)
            if meta.get('stop_reason'):
                stopped = True
            if projection is None or meta.get('projection_truncated'):
                entry['status'] = 'summary_unavailable'
                continue
            html = projected_html(projection).replace('<table>', '<table id="odds">', 1)
            summary = parse_opening_summary(html, event, fixture['home'], fixture['away'],
                                            fixture['kickoff_display'], fixture.get('company_ids', ['3']))
            entry['summary'] = summary
            if not summary['parse_ok']:
                entry['status'] = 'summary_identity_or_opening_failed'
                continue
            for company in summary['companies'].values():
                meta, projection = await asyncio.wait_for(read_page(context, company['history_url'], 'history', fixture), 45)
                entry['pages'].append(meta)
                if meta.get('stop_reason'):
                    stopped = True
                    break
                if projection is not None:
                    entry['histories'][company['company_id']] = prepare_archive(projection, fixture, company, meta)
            wanted = set(fixture.get('company_ids', ['3']))
            good = {k for k, a in entry['histories'].items() if a['parse_ok']
                    and a['fixture_identity_verified'] and a['provider_opening_reconciled']}
            entry['status'] = 'displayed_history_reconciled' if wanted == good else 'history_incomplete_or_failed'
        except Exception as exc:
            entry.update(status='read_or_parse_error', error_type=type(exc).__name__)


async def browser_run(targets, output):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        async with await p.chromium.launch() as browser:
            context = await browser.new_context()
            try:
                await collect(context, targets, output)
            finally:
                await context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--targets', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    targets = json.loads(args.targets.read_text(encoding='utf-8'))
    validate_targets(targets)
    output = {'schema_version': 'titan-bounded-batch-v1', 'started_at_utc': stamp(),
              'expected_fixture_count': len(targets), 'fixtures': [],
              'formal_observation': False, 'training_eligible': False,
              'complete_bookmaker_history_certified': False}
    try:
        asyncio.run(asyncio.wait_for(browser_run(targets, output), 480))
    except Exception as exc:
        output['batch_error_type'] = type(exc).__name__
    done = {r['fixture']['provider_event_id'] for r in output['fixtures']}
    for target in targets:
        if target['provider_event_id'] not in done:
            output['fixtures'].append({'fixture': target, 'status': 'not_attempted_budget_or_runtime_error'})
    output['finished_at_utc'] = stamp()
    output['reconciled_fixture_count'] = sum(f['status'] == 'displayed_history_reconciled' for f in output['fixtures'])
    output['data_acceptance_pass'] = output['reconciled_fixture_count'] == len(targets)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in output.items() if k != 'fixtures'}, ensure_ascii=False))
    for entry in output['fixtures']:
        print(json.dumps({'event': entry['fixture']['provider_event_id'], 'status': entry['status'],
                          'issues': entry.get('summary', {}).get('issues')}, ensure_ascii=False))
    # Source data failure is explicit in the artifact, not a repeated email.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
