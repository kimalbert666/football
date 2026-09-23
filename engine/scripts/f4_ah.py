"""Prospective, all-line Asian handicap research. Never issues betting advice."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from f4_ledger import load_latest_outcomes

HK = timezone(timedelta(hours=8))


def instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('timezone required')
    return result.astimezone(timezone.utc)


def stamp(value):
    return instant(value).isoformat().replace('+00:00', 'Z')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def write(path, value, immutable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if immutable:
        try:
            with path.open('x', encoding='utf-8') as stream:
                stream.write(body)
        except FileExistsError:
            if read(path) != value:
                raise ValueError('immutable AH record conflict')
    else:
        path.write_text(body, encoding='utf-8')


def load_observations(root):
    return sorted((read(p) for p in (root / 'data/f4/ah/observations').glob('*/*.json')),
                  key=lambda r: r['observed_at'])


def stage(fixture, now, cfg):
    if fixture.get('status') != 'scheduled' or not fixture.get('kickoff_at'):
        return None
    minutes = (instant(fixture['kickoff_at']) - now).total_seconds() / 60
    for target, tolerance in cfg['windows'].items():
        if minutes > 0 and abs(minutes - int(target)) <= tolerance:
            return f'T-{target}m'
    return None


def identity(row):
    return (row['match_id'], stamp(row['kickoff_at']), row['horizon'], row['experiment_id'])


def validate_quote(quote, fixture, decision, max_age, allowed_books=('crown',)):
    if not isinstance(quote, dict):
        raise ValueError('missing Asian handicap quote')
    if quote.get('bookmaker') not in allowed_books or quote.get('market') != '90min_asian_handicap':
        raise ValueError('wrong bookmaker or market')
    line, odds = quote.get('home_handicap'), quote.get('odds')
    if (isinstance(line, bool) or not isinstance(line, (int, float)) or not math.isfinite(line)
            or not math.isclose(line * 4, round(line * 4), abs_tol=1e-9)):
        raise ValueError('finite quarter-goal line required')
    if (not isinstance(odds, list) or len(odds) != 2
            or any(isinstance(o, bool) or not isinstance(o, (int, float)) or not math.isfinite(o) or o <= 1 for o in odds)):
        raise ValueError('two decimal odds required')
    captured = instant(quote['captured_at'])
    if not 0 <= (decision - captured).total_seconds() <= max_age * 60:
        raise ValueError('AH capture stale or future')
    if decision >= instant(fixture['kickoff_at']):
        raise ValueError('pre-match deadline missed')
    if quote.get('published_at') and instant(quote['published_at']) > captured:
        raise ValueError('AH quote publication after capture')
    if not quote.get('url') or not quote.get('provider_event_id') or not quote.get('content_sha256'):
        raise ValueError('AH provenance missing')
    return quote


def probability_1x2(market, decision, max_age, bookmaker='crown'):
    try:
        if market.get('bookmaker') != bookmaker:
            return None
        if not 0 <= (decision - instant(market['captured_at'])).total_seconds() <= max_age * 60:
            return None
        if market.get('published_at') and instant(market['published_at']) > decision:
            return None
        odds = market['odds']
        if len(odds) != 3 or any(isinstance(o, bool) or not math.isfinite(o) or o <= 1 for o in odds):
            return None
        inverse = [1 / o for o in odds]
        return [o / sum(inverse) for o in inverse]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def ratings_before(outcomes, fixture, decision):
    """Only already observed regulation results can update this declared Elo."""
    ratings, counts = defaultdict(lambda: 1500.0), Counter()
    latest = None
    for row in sorted(outcomes.values(), key=lambda r: r['kickoff_at']):
        if (row.get('status') != 'completed' or instant(row['observed_at']) >= decision
                or instant(row['kickoff_at']) >= decision):
            continue
        h, a = row['home_id'], row['away_id']
        expected = 1 / (1 + 10 ** ((ratings[a] - ratings[h] - 60) / 400))
        hg, ag = row['home_score'], row['away_score']
        actual = 1.0 if hg > ag else (0.5 if hg == ag else 0.0)
        move = 20 * (actual - expected)
        ratings[h] += move
        ratings[a] -= move
        counts[h] += 1
        counts[a] += 1
        latest = max(latest or row['observed_at'], row['observed_at'])
    h, a = fixture['home_id'], fixture['away_id']
    if min(counts[h], counts[a]) < 5:
        return None
    return {'home': ratings[h], 'away': ratings[a], 'observed_at': latest,
            'source': 'verified-observed-results-elo-k20-ha60-v1',
            'prior_games': [counts[h], counts[a]]}


def paired_rows(root, observations, now):
    outcomes = load_latest_outcomes(root)
    paired, reasons, seen = [], Counter(), set()
    # The earliest valid T-15 snapshot is the fixed evaluation point.
    for row in observations:
        if row['horizon'] != 'T-15m' or not row.get('quote'):
            continue
        key = (row['match_id'], row['kickoff_at'], row['experiment_id'])
        if key in seen:
            continue
        seen.add(key)
        outcome = outcomes.get(row['match_id'])
        if not outcome or outcome['status'] != 'completed':
            reasons['awaiting_regulation_result'] += 1
            continue
        if any(row.get(k) != outcome.get(k) for k in ('home_id', 'away_id', 'kickoff_at')):
            reasons['outcome_identity_changed'] += 1
            continue
        if not instant(row['observed_at']) < instant(row['kickoff_at']) < instant(outcome['observed_at']) <= now:
            reasons['invalid_result_timing'] += 1
            continue
        paired.append({**row, 'home_handicap': row['quote']['home_handicap'], 'odds': row['quote']['odds'],
                       'final_score': [outcome['home_score'], outcome['away_score']],
                       'settled_at': outcome['observed_at']})
    return paired, dict(reasons)


def grouped_results(rows):
    from f4_ah_research import settle_asian_handicap
    groups = {}
    for row in rows:
        line = str(float(row['home_handicap']))
        bucket = groups.setdefault(line, {'matches': 0, 'home': Counter(), 'away': Counter(),
                                         'home_profit': 0.0, 'away_profit': 0.0})
        bucket['matches'] += 1
        for index, side in enumerate(('home', 'away')):
            result = settle_asian_handicap(*row['final_score'], row['home_handicap'], side, row['odds'][index])
            bucket[side][result['settlement']] += 1
            bucket[side + '_profit'] += result['net_profit']
    return groups


def prospective_scores(rows):
    """Score only model outputs actually stored before kickoff, by fixed version."""
    from f4_ah_research import settle_asian_handicap
    groups = {}
    for row in sorted(rows, key=lambda r: (r['kickoff_at'], r['match_id'])):
        for candidate_id, record in row.get('candidates', {}).items():
            prediction = record.get('prediction')
            if not prediction:
                continue
            key = candidate_id + ':' + record['model_version']
            group = groups.setdefault(key, {'predicted_matches': 0, 'shadow_selections': 0,
                                           'settlements': Counter(), 'net_profit': 0.0,
                                           'same_subset_lower_price_profit': 0.0, 'max_drawdown': 0.0,
                                           'log_loss_sum': 0.0, '_peak': 0.0})
            group['predicted_matches'] += 1
            home_result = settle_asian_handicap(*row['final_score'], row['home_handicap'], 'home', row['odds'][0])
            group['log_loss_sum'] -= math.log(max(prediction['home']['probabilities'][home_result['settlement']], 1e-15))
            side = record.get('shadow_direction')
            if side not in ('home', 'away'):
                continue
            result = settle_asian_handicap(*row['final_score'], row['home_handicap'], side,
                                           row['odds'][0 if side == 'home' else 1])
            reference = 'home' if row['odds'][0] <= row['odds'][1] else 'away'
            baseline = settle_asian_handicap(*row['final_score'], row['home_handicap'], reference,
                                             row['odds'][0 if reference == 'home' else 1])
            group['shadow_selections'] += 1
            group['settlements'][result['settlement']] += 1
            group['net_profit'] += result['net_profit']
            group['same_subset_lower_price_profit'] += baseline['net_profit']
            group['_peak'] = max(group['_peak'], group['net_profit'])
            group['max_drawdown'] = max(group['max_drawdown'], group['_peak'] - group['net_profit'])
    for group in groups.values():
        group['log_loss'] = group.pop('log_loss_sum') / group['predicted_matches']
        group['coverage'] = group['shadow_selections'] / group['predicted_matches']
        group['roi'] = group['net_profit'] / group['shadow_selections'] if group['shadow_selections'] else None
        group.pop('_peak')
    return groups


def update_batches(root, fixtures, observations, now, cfg):
    """Audit the full fixture universe, including missing final-stage observations."""
    by_key = defaultdict(list)
    for item in observations:
        by_key[(item['match_id'], item['kickoff_at'])].append(item)
    batches = defaultdict(list)
    for fixture in fixtures:
        if fixture.get('kickoff_at') and fixture.get('home_id') and fixture.get('away_id'):
            batches[fixture['kickoff_at']].append(fixture)
    for kickoff, rows in batches.items():
        records = []
        for fixture in rows:
            finals = [r for r in by_key[(fixture['match_id'], kickoff)] if r['horizon'] == 'T-15m']
            valid = next((r for r in finals if r.get('quote')), None)
            if valid:
                status = 'recorded_research_only'
            elif now >= instant(kickoff) - timedelta(minutes=10):
                status = 'data_unavailable' if finals else 'missed_window'
            else:
                status = 'awaiting_window'
            records.append({'match_id': fixture['match_id'], 'home': fixture['home'], 'away': fixture['away'],
                            'status': status, 'observation_id': valid.get('observation_id') if valid else None})
        value = {'kickoff_at': kickoff, 'updated_at': stamp(now), 'fixtures': records,
                 'research_only': True, 'direction_notification_enabled': False,
                 'all_no_selection': False, 'reason': 'No approved models; missing data is not a no-pick verdict'}
        write(root / 'data/f4/ah/batches' / (digest(kickoff)[:24] + '.json'), value)


def render_ah(summary):
    text = '\n\n## 亚洲让球研究\n\n'
    text += ('覆盖所有实际出现的合法让球档位；盘口示例不是筛选白名单。研究期只发月报和重要故障，试验方向后台保存。\n\n')
    text += (f"- 有效亚盘记录：{summary['valid_observations']}；涉及{summary['distinct_matches']}场比赛。\n"
             f"- T−15分钟已有赛果配对：{summary['paired_matches']}场。\n"
             '- 全赢、半赢、走盘、半输、全输分开；按赛前保存的价格结算。\n'
             '- 候选M1盘口与水位、M2实力与盘口差异、M3盘口变化；缺样本或特征时不训练、不虚构命中率。\n')
    for key, item in summary.get('research', {}).get('candidates', {}).items():
        text += f"- {key}：{item.get('status', 'unknown')}。\n"
    for problem in summary.get('problems', []):
        text += f'- 亚盘数据提醒：{problem}\n'
    return text


def run_ah(root, fixtures, command, *, clock, source=None):
    config = read(root / 'data/f4/config.json', {})
    cfg = config.get('asian_handicap', {})
    if not cfg.get('enabled'):
        return None
    if not cfg.get('research_only') or cfg.get('automatic_promotion') or cfg.get('automatic_wagering'):
        raise ValueError('AH research requires research-only, no promotion and no wagering')
    from f4_ah_source import attach_crown_ah
    from f4_ah_research import research, predict
    source = source or attach_crown_ah
    now, old = clock(), load_observations(root)
    valid_keys = {identity(r) for r in old if r.get('quote')}
    due = []
    if command in ('capture', 'all'):
        for fixture in fixtures:
            horizon = stage(fixture, now, cfg)
            if (horizon and fixture.get('home_id') and fixture.get('away_id')
                    and (fixture['match_id'], stamp(fixture['kickoff_at']), horizon, cfg['experiment_id']) not in valid_keys):
                due.append(fixture)
    previous_source = read(root / 'data/f4/ah/source-health.json', {})
    should_probe = command in ('daily', 'all', 'monthly')
    if due or should_probe:
        bundle = source(root, due, now, probe=should_probe)
        write(root / 'data/f4/ah/source-health.json', {'checked_at': stamp(clock()), **bundle})
    else:
        bundle = {**previous_source, 'markets': {}}
    outcomes = load_latest_outcomes(root)
    model_report = read(root / 'data/f4/ah/research/latest.json', {})
    new_valid, new_attempts = 0, 0
    for fixture in due:
        decision = clock()
        horizon = stage(fixture, decision, cfg)
        if horizon is None:
            continue
        reason, quote = None, bundle.get('markets', {}).get(fixture['match_id'])
        try:
            quote = validate_quote(quote, fixture, decision, cfg['max_capture_age_minutes'], cfg.get('bookmaker_priority', ['crown']))
        except (KeyError, TypeError, ValueError) as exc:
            quote, reason = None, str(exc)
        early = next((r for r in old if r['match_id'] == fixture['match_id']
                      and r['kickoff_at'] == fixture['kickoff_at'] and r.get('quote') and quote
                      and r['quote']['bookmaker'] == quote['bookmaker']
                      and instant(r['observed_at']) < decision), None)
        early_quote = ({'observed_at': early['observed_at'], 'home_handicap': early['quote']['home_handicap'],
                        'odds': early['quote']['odds'], 'bookmaker': early['quote']['bookmaker'],
                        'provenance': early['provenance']} if early else None)
        row = {key: fixture.get(key) for key in ('match_id', 'provider', 'source_event_id', 'home_id', 'away_id', 'home', 'away', 'kickoff_at', 'league')}
        joint_market = (quote or {}).get('market_1x2') or fixture.get('market')
        row.update(schema_version=1, experiment_id=cfg['experiment_id'], observed_at=stamp(decision),
                   horizon=horizon, actual_minutes=(instant(fixture['kickoff_at']) - decision).total_seconds() / 60,
                   quote=quote, bookmaker=(quote or {}).get('bookmaker'),
                   p_1x2=probability_1x2(joint_market, decision, cfg['max_capture_age_minutes'], (quote or {}).get('bookmaker', 'crown')),
                   quote_1x2=joint_market, ratings=ratings_before(outcomes, fixture, decision),
                   early_quote=early_quote, reason=reason, selected=False, candidate_weight=0,
                   provenance={'kind': 'prospective_capture', 'market': '90min_asian_handicap',
                               'source': quote.get('url') if quote else None}, candidates={})
        if quote:
            prediction_input = {**row, 'home_handicap': quote['home_handicap'], 'odds': quote['odds']}
            for key, candidate in model_report.get('candidates', {}).items():
                model = candidate.get('model')
                if not model or instant(model_report['evaluated_at']) >= decision:
                    continue
                try:
                    prediction = predict(model, prediction_input)
                    side = max(('home', 'away'), key=lambda s: prediction[s]['ev'])
                    row['candidates'][key] = {'model_version': model['model_version'], 'prediction': prediction,
                                              'shadow_direction': side if prediction[side]['ev'] >= .02 else None,
                                              'diagnostic_ev_threshold': .02}
                except (ValueError, KeyError, TypeError) as exc:
                    row['candidates'][key] = {'unavailable': type(exc).__name__}
        generated = clock()
        if generated >= instant(fixture['kickoff_at']):
            continue
        row['generated_at'] = stamp(generated)
        row['observation_id'] = digest([identity(row), row['observed_at']])
        write(root / 'data/f4/ah/observations' / row['kickoff_at'][:10] / (row['observation_id'] + '.json'), row, True)
        old.append(row)
        new_attempts += 1
        new_valid += int(quote is not None)
    paired, pairing_issues = paired_rows(root, old, clock())
    # One new research version per calendar month. Reused retrospective tests
    # are exploratory; only stored future predictions are prospective evidence.
    month = clock().astimezone(HK).strftime('%Y-%m')
    research_path = root / 'data/f4/ah/research' / (month + '.json')
    if command in ('daily', 'all', 'monthly', 'review') and not research_path.exists():
        report = research(paired, now=clock())
        report = {**report, 'evaluated_at': stamp(clock()), 'research_month': month,
                  'prospective_evidence': False, 'automatic_promotion': False,
                  'note': 'Rolling retrospective model development, not proof of a prospective advantage'}
        write(research_path, report, True)
        write(root / 'data/f4/ah/research/latest.json', report)
        model_report = report
    update_batches(root, fixtures, old, clock(), cfg)
    summary = {'evaluated_at': stamp(clock()), 'new_valid': new_valid, 'new_attempts': new_attempts,
               'valid_observations': sum(r.get('quote') is not None for r in old),
               'distinct_matches': len({r['match_id'] for r in old if r.get('quote')}),
               'paired_matches': len(paired), 'pairing_issues': pairing_issues,
               'line_groups': grouped_results(paired), 'prospective_scores': prospective_scores(paired), 'research': model_report,
               'by_bookmaker': {book: grouped_results([r for r in paired if r['quote']['bookmaker'] == book])
                               for book in sorted({r['quote']['bookmaker'] for r in paired})},
               'problems': bundle.get('problems', []), 'sources': bundle.get('sources', []),
               'research_only': True, 'automatic_promotion': False, 'automatic_wagering': False}
    write(root / 'data/f4/ah/latest.json', summary)
    return summary


def archive_month(root, now, ah_summary):
    config = read(root / 'data/f4/config.json', {})
    start = instant(config['notifications']['started_at']).astimezone(HK).strftime('%Y-%m')
    local = now.astimezone(HK)
    # Let late evening month-end fixtures finish and the daily result task run.
    if local.day == 1 and (local.hour, local.minute) < (10, 13):
        return {}
    month = (local.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    if month < start:
        return {}
    path = root / 'data/f4/reports' / ('monthly-' + month + '.md')
    if not path.exists():
        observations = [r for r in load_observations(root)
                        if instant(r['kickoff_at']).astimezone(HK).strftime('%Y-%m') == month]
        paired, issues = paired_rows(root, observations, now)
        fixtures = [read(p) for p in (root / 'data/f4/fixtures').glob('*.json')]
        fixture_ids = {r['match_id'] for r in fixtures if r.get('kickoff_at')
                       and instant(r['kickoff_at']).astimezone(HK).strftime('%Y-%m') == month}
        fixture_ids.update(r['match_id'] for r in observations)
        recorded_ids = {r['match_id'] for r in observations if r.get('quote') and r['horizon'] == 'T-15m'}
        groups = grouped_results(paired)
        text = f'# f4 英超亚洲让球月报：{month}\n\n'
        text += f"生成时间：{now.astimezone(HK):%Y-%m-%d %H:%M}（香港时间）。\n\n"
        text += ('研究范围为所有实际出现的让球盘；目前为研究期，不发试验方向。'
                 '以下上下盘收益是按当时价格分别全选的市场对照，不是模型命中成绩。\n\n')
        text += f'当月有效亚盘记录：{sum(r.get("quote") is not None for r in observations)}条；T−15分钟赛果配对：{len(paired)}场。\n\n'
        text += f'已识别当月赛程：{len(fixture_ids)}场；具有有效T−15报价：{len(recorded_ids)}场；缺报价／漏窗：{len(fixture_ids - recorded_ids)}场。赛程自身未必完整，以上为已观察覆盖。\n\n'
        text += '| 主队视角盘口 | 比赛数 | 主队全／半赢／走／半输／全输 | 客队对应结算 | 主队模拟净收益 | 客队模拟净收益 |\n|---|---|---|---|---|---|\n'
        classes = ('full_win', 'half_win', 'push', 'half_loss', 'full_loss')
        for line, item in sorted(groups.items(), key=lambda pair: float(pair[0])):
            h = '/'.join(str(item['home'].get(c, 0)) for c in classes)
            a = '/'.join(str(item['away'].get(c, 0)) for c in classes)
            text += f"| {line} | {item['matches']} | {h} | {a} | {item['home_profit']:.3f} | {item['away_profit']:.3f} |\n"
        text += '\n每方向每场模拟1单位；半赢／走盘不冒充全赢。样本不足不能证明模型优势。\n'
        text += f'\n未配对原因：{json.dumps(issues, ensure_ascii=False)}。\n'
        scores = prospective_scores(paired)
        text += '\n## 真实赛前模型对照\n\n'
        if not scores:
            text += '当月没有可结算的赛前模型输出；不将新训练模型的历史测试成绩冒充真实预测。\n'
        for version, metrics in scores.items():
            text += f'\n- {version}：{json.dumps(metrics, ensure_ascii=False)}\n'
        if ah_summary:
            text += render_ah(ah_summary)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return {month: hashlib.sha256(path.read_bytes()).hexdigest()}
