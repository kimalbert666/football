"""Run the fixed f4 forward experiment, alongside the existing monitor.

No wagers, fitting, automatic weight changes, or language-model API calls.
Only data/f4 and temporary diagnostics are written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from f4_sources import collect
from f4_candidates import dc_candidate, capture_f2
from f4_ledger import load_predictions, load_latest_outcomes, save_prediction, save_outcome, evaluate_ledger, render_report

ROOT = Path(__file__).resolve().parents[2]


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def instant(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('timezone required')
    return parsed.astimezone(timezone.utc)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def outputs(**values):
    target = os.getenv('GITHUB_OUTPUT')
    if target:
        with open(target, 'a', encoding='utf-8') as stream:
            for key, value in values.items():
                stream.write(f'{key}={str(value).lower() if isinstance(value, bool) else value}\n')


def market_probability(fixture, decision_at, config):
    market = fixture.get('market')
    if not market:
        return None, '缺少匹配的在售胜平负赔率'
    try:
        captured = instant(market['captured_at'])
        age = (decision_at - captured).total_seconds() / 60
        if not 0 <= age <= config['max_capture_age_minutes']:
            raise ValueError('quote capture outside freshness window')
        if market.get('published_at') and instant(market['published_at']) > decision_at:
            raise ValueError('quote update timestamp is in the future')
        odds = market['odds']
        if len(odds) != 3 or any(isinstance(v, bool) for v in odds):
            raise ValueError('three numeric odds required')
        odds = [float(v) for v in odds]
        if any(not math.isfinite(v) or v <= 1 for v in odds):
            raise ValueError('invalid odds')
        inverse = [1 / v for v in odds]
        return [v / sum(inverse) for v in inverse], None
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return None, f'赔率不可用：{exc}'


def stage_for(fixture, now, config):
    if fixture.get('status') != 'scheduled':
        return None
    try:
        minutes = (instant(fixture['kickoff_at']) - now).total_seconds() / 60
    except (KeyError, ValueError, TypeError):
        return None
    for target in config['horizons_minutes']:
        if minutes > 0 and abs(minutes - target) <= config['horizon_tolerance_minutes']:
            return f'T-{target}m'
    return None


def prediction_key(row):
    return (row['match_id'], stamp(instant(row['kickoff_at'])), row['horizon'], row['experiment_id'])


def run(root, command, *, collector=collect, candidate=dc_candidate, f2_fetcher=capture_f2,
        clock=lambda: datetime.now(timezone.utc)):
    config = read_json(root / 'data/f4/config.json', None)
    if not config or config['mode'] != 'shadow' or config['candidate_weight'] != 0:
        raise ValueError('Only the frozen zero-weight shadow experiment is supported')
    now = clock()
    old = load_predictions(root)
    tracked_config = read_json(root / config['tracked_teams_path'], {})
    tracked = {t['canonicalId'] for t in tracked_config.get('teams', [])}
    league_scope = config.get('scope') == 'league'
    if not league_scope and not tracked:
        raise ValueError('Tracked team configuration is empty')
    capture = command in ('capture', 'all')
    settle = command in ('daily', 'all')
    review = command in ('review', 'all')
    pending = {}
    if settle:
        outcomes = load_latest_outcomes(root)
        for record in old:
            if (instant(record['kickoff_at']) < now
                    and outcomes.get(record['match_id'], {}).get('status') not in ('completed', 'cancelled')):
                pending[record['match_id']] = record
    if capture or settle:
        bundle = collector(root, now, include_market=capture, pending=list(pending.values()),
                           lookahead_days=7 if settle else 1, lookback_days=1 if settle else 0)
    else:
        bundle = read_json(root / 'data/f4/status/sources-latest.json',
                           {'captured_at': None, 'fixtures': [], 'sources': [], 'coverage': {}, 'problems': ['尚无数据采集记录']})
    fixtures = [f for f in bundle.get('fixtures', [])
                if f.get('league') == config['league']
                and (league_scope or f.get('home_id') in tracked or f.get('away_id') in tracked)]
    if capture or settle:
        write_json(root / 'data/f4/status/sources-latest.json', bundle)
        for fixture in fixtures:
            identity = fingerprint(fixture['match_id'])[:24]
            path = root / 'data/f4/fixtures' / f'{identity}.json'
            previous = read_json(path, {})
            write_json(path, {**fixture, 'first_seen_at': previous.get('first_seen_at', bundle['captured_at']),
                              'last_seen_at': bundle['captured_at']})
    seen = {prediction_key(r) for r in old if r.get('p_baseline') is not None}
    due = []
    if capture:
        for fixture in fixtures:
            stage = stage_for(fixture, clock(), config)
            if not stage or not fixture.get('home_id') or not fixture.get('away_id'):
                continue
            key = (fixture['match_id'], stamp(instant(fixture['kickoff_at'])), stage, config['experiment_id'])
            if key not in seen:
                due.append(fixture)
    f2 = f2_fetcher(root, due) if due else {'sources': [], 'predictions': {}, 'problems': []}
    new_predictions, new_valid, new_outcomes, skipped = 0, 0, 0, []
    for fixture in due:
        decision = clock()
        stage = stage_for(fixture, decision, config)
        if not stage:
            skipped.append(f"{fixture['match_id']}: 数据返回时已离开预定窗口")
            continue
        p_market, reason = market_probability(fixture, decision, config)
        dc = candidate(root, fixture, decision)
        rejected_cutoff = None
        if dc.get('trained_through'):
            try:
                if instant(dc['trained_through']) >= decision:
                    raise ValueError('training cutoff is not before decision')
            except (TypeError, ValueError):
                rejected_cutoff = dc['trained_through']
                dc = {**dc, 'trained_through': None, 'p_candidate': None,
                      'reason': dc.get('reason') or '训练截止无效，候选排除'}
        point = f2.get('predictions', {}).get(fixture['match_id'])
        captures = [bundle['captured_at']]
        if fixture.get('market'):
            captures.append(fixture['market']['captured_at'])
        if point:
            captures.append(point['captured_at'])
        captured = max(instant(t) for t in captures)
        if captured > decision:
            skipped.append(f"{fixture['match_id']}: 输入抓取时间晚于决策时间")
            continue
        generated = clock()
        if generated >= instant(fixture['kickoff_at']):
            skipped.append(f"{fixture['match_id']}: 开赛前未完成记录")
            continue
        record = {k: fixture.get(k) for k in ('match_id', 'source_event_id', 'provider', 'league',
                    'home_id', 'away_id', 'home', 'away', 'kickoff_at')}
        record.update({
            'schema_version': 1, 'experiment_id': config['experiment_id'], 'market': '90min_1x2',
            'decision_at': stamp(decision), 'captured_at': stamp(captured), 'generated_at': stamp(generated),
            'horizon': stage, 'horizon_tolerance_minutes': config['horizon_tolerance_minutes'],
            'actual_minutes': (instant(fixture['kickoff_at']) - decision).total_seconds() / 60,
            'p_baseline': p_market, 'p_champion': p_market, 'p_candidate': dc.get('p_candidate'),
            'candidate_version': dc.get('candidate_version') or 'unavailable',
            'trained_through': dc.get('trained_through'), 'candidate_training_evidence': dc.get('training_evidence'),
            'rejected_candidate_cutoff': rejected_cutoff,
            'candidate_source_sha256': dc.get('source_sha256'), 'candidate_reason': dc.get('reason'),
            'baseline_version': 'sporttery-had-inverse-normalized-v1', 'champion_version': 'market-reference-v1',
            'selected': False, 'candidate_weight': 0,
            'reason': reason or '仅作单源市场参考，候选尚未验证优势，不输出投注建议',
            'quote': fixture.get('market'), 'f2': point,
            'sources': bundle.get('sources', []) + f2.get('sources', []),
            'source_problems': bundle.get('problems', []) + fixture.get('problems', []) + f2.get('problems', []),
            'coverage': bundle.get('coverage', {}),
            'origin': {'kind': 'github-actions' if os.getenv('GITHUB_ACTIONS') == 'true' else 'local',
                       'commit': os.getenv('GITHUB_SHA'), 'run_id': os.getenv('GITHUB_RUN_ID')},
        })
        _, created = save_prediction(root, record)
        new_predictions += int(created)
        new_valid += int(created and p_market is not None)
    if settle:
        observed = stamp(clock())
        for fixture in fixtures:
            if not all(fixture.get(key) for key in ('home_id', 'away_id', 'kickoff_at')):
                continue
            if fixture.get('status') not in ('completed', 'postponed', 'cancelled'):
                # A rescheduled kickoff must invalidate the old stage even before it is played.
                prior = pending.get(fixture['match_id'])
                if not prior or instant(prior['kickoff_at']) == instant(fixture['kickoff_at']):
                    continue
            score = fixture.get('score') or [None, None]
            outcome = {**fixture, 'home_score': score[0], 'away_score': score[1]}
            new_outcomes += int(save_outcome(root, outcome, observed))
    completed_at = clock()
    summary = evaluate_ledger(root, completed_at)
    source_health = {'coverage': bundle.get('coverage', {}), 'problems': bundle.get('problems', []),
                     'sources': bundle.get('sources', [])}
    status = {'checked_at': stamp(completed_at), 'command': command, 'mode': 'shadow',
              'new_predictions': new_predictions, 'new_valid_predictions': new_valid,
              'new_outcomes': new_outcomes, 'due_fixtures': len(due), 'tracked_fixtures_seen': len(fixtures),
              'skipped': skipped, 'source_health': source_health, 'f2_problems': f2.get('problems', []),
              'automatic_promotion': False, 'automatic_wagering': False}
    write_json(root / 'data/f4/status/latest.json', status)
    write_json(root / 'data/f4/evaluations/latest.json', summary)
    report = render_report(summary)
    report += '\n\n## 本次运行\n\n'
    scope_label = '全英超' if league_scope else '配置中的英超球队'
    report += f"- 核对时间：{stamp(completed_at)}（UTC）。\n- 范围：{scope_label}，杯赛尚未接入。\n"
    report += f'- 新记录 {new_predictions} 条，其中市场输入有效 {new_valid} 条；赛果／状态修订 {new_outcomes} 条。\n'
    report += '- 赛前窗口：T−180分与T−60分，各允许±20分；实际时间保存在每条记录中。\n'
    report += '- f4与原任务并行；候选权重为0，不自动下注、不自动读论文或改权重。\n'
    report += '- 当前候选：冻结的旧DC参数，仅用于前向对照；其历史训练资料尚未独立认证。\n'
    report += '- 赔率为体彩单源；抓取时间和上次价格变化时间不能单独证明提供商内部数据最新。\n'
    if not fixtures:
        report += '- 本次数据范围未识别到目标赛程；这不代表目标联赛一定没有比赛。\n'
    for issue in [*bundle.get('problems', []), *f2.get('problems', []), *skipped][:20]:
        report += f'- 数据提醒：{str(issue)}\n'
    if not bundle.get('coverage', {}).get('complete', False):
        report += '- 赛程完整性未确认，不能将缺失比赛视为已覆盖。\n'
    recent = sorted(load_predictions(root), key=lambda r: instant(r['decision_at']), reverse=True)[:8]
    if recent:
        report += '\n## 最近赛前记录\n\n| 比赛 | 窗口 | 市场参考：主／平／客 | f2比分点预测 | 状态 |\n|---|---|---|---|---|\n'
        for row in recent:
            probabilities = row.get('p_baseline')
            probability_text = '／'.join(f'{p:.1%}' for p in probabilities) if probabilities else '缺失'
            point = (row.get('f2') or {}).get('score')
            point_text = f'{point[0]}:{point[1]}' if point else '未取得可匹配预测'
            label = f"{row.get('home')} — {row.get('away')}"
            report += f"| {label} | {row['horizon']} | {probability_text} | {point_text} | 仅记录／无投注建议 |\n"
    report_path = root / 'data/f4/reports/latest.md'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.rstrip() + '\n', encoding='utf-8')
    if review:
        iso = completed_at.isocalendar()
        report_path.with_name(f'{iso.year}-W{iso.week:02d}.md').write_text(report, encoding='utf-8')
    # Health digest excludes capture timestamps, but preserves changes in error classes/coverage.
    health_digest = fingerprint({'complete': bundle.get('coverage', {}).get('complete'),
                                 'problems': bundle.get('problems', [])})
    stable_summary = {key: value for key, value in summary.items() if key != 'evaluated_at'}
    last_notice = read_json(root / 'data/f4/status/notification.json', {})
    last_target = read_json(root / 'data/f4/status/notification-latest.json', last_notice)
    review_week = completed_at.strftime('%G-W%V') if review else last_target.get('review_week')
    token = fingerprint({'experiment': config['experiment_id'], 'predictions': len(load_predictions(root)),
                         'summary': stable_summary, 'health': health_digest,
                         'review_week': review_week})
    # A failed delivery leaves the acknowledgement unchanged, even if records
    # were persisted; the next run therefore retries the meaningful change.
    notify = token != last_notice.get('token')
    logdir = root / '.github/run-logs'
    notice = {'token': token, 'health_digest': health_digest, 'review_week': review_week}
    write_json(logdir / 'f4-notify-token.json', notice)
    write_json(root / 'data/f4/status/notification-latest.json', notice)
    (logdir / 'f4-issue.md').write_text(report + f'\n<!-- f4-notification:{token} -->\n', encoding='utf-8')
    outputs(notify=notify, notification_marker=f'f4-notification:{token}',
            report='.github/run-logs/f4-issue.md')
    if os.getenv('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write(report)
    if bundle.get('problems') or not bundle.get('coverage', {}).get('complete', False):
        print('::warning::f4 数据覆盖存在缺口，请阅读运行摘要；成功退出不代表数据齐全。')
    print(json.dumps(status, ensure_ascii=False, allow_nan=False))
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('capture', 'daily', 'review', 'all', 'notify-ack'))
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    if args.command == 'notify-ack':
        notice = read_json(args.root / '.github/run-logs/f4-notify-token.json', None)
        if not notice:
            raise ValueError('No successful notification token to acknowledge')
        write_json(args.root / 'data/f4/status/notification.json', notice)
    else:
        run(args.root, args.command)


if __name__ == '__main__':
    main()
