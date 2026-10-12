"""Descriptive fixed-T15 handicap/price/venue tables, never model performance."""
from __future__ import annotations

from collections import Counter

from f4_ah_research import CLASSES, _line, _pair, _price, _time, settle_asian_handicap

PROTOCOL_ID = 'f4-ah-price-venue-v1'
ODDS_BANDS = (
    {'id': 'below_1_75', 'label': '<1.75', 'lower': 1, 'lower_inclusive': False,
     'upper': 1.75, 'upper_inclusive': False},
    {'id': '1_75_to_1_85', 'label': '1.75–1.85', 'lower': 1.75, 'lower_inclusive': True,
     'upper': 1.85, 'upper_inclusive': True},
    {'id': 'above_1_85_below_1_90', 'label': '>1.85且<1.90', 'lower': 1.85,
     'lower_inclusive': False, 'upper': 1.90, 'upper_inclusive': False},
    {'id': '1_90_to_2_00', 'label': '1.90–2.00', 'lower': 1.90, 'lower_inclusive': True,
     'upper': 2.00, 'upper_inclusive': True},
    {'id': 'above_2_00', 'label': '>2.00', 'lower': 2.00, 'lower_inclusive': False,
     'upper': None, 'upper_inclusive': False},
)
_BAND_ORDER = {band['id']: index for index, band in enumerate(ODDS_BANDS)}
_BAND_LABEL = {band['id']: band['label'] for band in ODDS_BANDS}
_SIDE_LABEL = {'upper': '上盘', 'lower': '下盘', 'flat_home': '平手主队', 'flat_away': '平手客队'}
_VENUE_LABEL = {'home': '主场', 'away': '客场'}


def odds_band(decimal_odds):
    """Return the exact non-overlapping user-requested decimal-odds interval."""
    price = _price(decimal_odds)
    if price < 1.75:
        return 'below_1_75'
    if price <= 1.85:
        return '1_75_to_1_85'
    if price < 1.90:
        return 'above_1_85_below_1_90'
    if price <= 2.00:
        return '1_90_to_2_00'
    return 'above_2_00'


def _valid_pair(row):
    if not isinstance(row, dict):
        raise ValueError('paired row must be an object')
    for field in ('match_id', 'experiment_id', 'home_id', 'away_id'):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f'{field} required')
    if row['home_id'] == row['away_id']:
        raise ValueError('home and away identities must differ')
    observed, kickoff, settled = (_time(row.get(field)) for field in
                                  ('observed_at', 'kickoff_at', 'settled_at'))
    if row.get('horizon') != 'T-15m' or not 10 <= (kickoff - observed).total_seconds() / 60 <= 20:
        raise ValueError('fixed T-15 pre-match window required')
    if settled <= kickoff:
        raise ValueError('result availability must follow kickoff')
    line = _line(row.get('home_handicap'))
    prices, score = _pair(row.get('odds'), price=True), _pair(row.get('final_score'), score=True)
    quote = row.get('quote')
    if not isinstance(quote, dict) or not quote:
        raise ValueError('paired quote must be a nonempty object')
    book = quote.get('bookmaker')
    if not isinstance(book, str) or not book.strip():
        raise ValueError('bookmaker required')
    if row.get('bookmaker') and row['bookmaker'] != book:
        raise ValueError('bookmaker identity mismatch')
    if quote.get('market') != '90min_asian_handicap':
        raise ValueError('regulation Asian handicap required')
    if _line(quote.get('home_handicap')) != line:
        raise ValueError('stored handicap differs from quote')
    if _pair(quote.get('odds'), price=True) != prices:
        raise ValueError('stored prices differ from quote')
    return {**row, 'bookmaker': book, 'home_handicap': line, 'odds': prices, 'final_score': score,
            'observed_at': observed.isoformat(), 'kickoff_at': kickoff.isoformat(),
            'settled_at': settled.isoformat()}


def _bucket(**fields):
    return {**fields, 'matches': 0, 'settlement_counts': {name: 0 for name in CLASSES},
            'unit_stake_net_profit': 0.0, 'records': []}


def _record(bucket, row, venue, result):
    index = 0 if venue == 'home' else 1
    bucket['matches'] += 1
    bucket['settlement_counts'][result['settlement']] += 1
    bucket['unit_stake_net_profit'] += result['net_profit']
    bucket['records'].append({
        'match_id': row['match_id'], 'kickoff_at': row['kickoff_at'],
        'observed_at': row['observed_at'], 'home': row.get('home', row['home_id']),
        'away': row.get('away', row['away_id']), 'team': row.get(venue, row[venue + '_id']),
        'home_handicap': row['home_handicap'], 'venue': venue,
        'decimal_odds': row['odds'][index], 'final_score': list(row['final_score']),
        'settlement': result['settlement'], 'unit_stake_net_profit': result['net_profit'],
    })


def _finalize(bucket):
    count, settlements = bucket['matches'], bucket['settlement_counts']
    bucket['positive_returns'] = settlements['full_win'] + settlements['half_win']
    bucket['positive_return_rate'] = bucket['positive_returns'] / count if count else None
    bucket['full_win_rate'] = settlements['full_win'] / count if count else None
    bucket['unit_stake_net_profit'] = round(bucket['unit_stake_net_profit'], 10)
    bucket['unit_stake_roi'] = bucket['unit_stake_net_profit'] / count if count else None
    return bucket


def price_venue_diagnostics(rows):
    """Summarize paired rows without pooling bookmakers, experiments or venues.

    Upstream paired_rows audits the source/outcome identity. This function also
    checks the fixed T15 contract and keeps the earliest valid observation for
    each experiment/bookmaker/fixture/kickoff. Two sides remain related outcomes,
    not two independent games. Results describe recorded contracts, not forecasts.
    """
    valid, rejected = [], []
    for row in rows:
        try:
            valid.append(_valid_pair(row))
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append({'match_id': row.get('match_id') if isinstance(row, dict) else None,
                             'reason': str(exc)})
    # Normalize instants before sorting: ISO offsets must not order snapshots.
    valid.sort(key=lambda row: (_time(row['observed_at']), row['experiment_id'],
                               row['bookmaker'], row['match_id'], row['kickoff_at']))
    accepted, seen, duplicates = [], set(), 0
    for row in valid:
        key = (row['experiment_id'], row['bookmaker'], row['match_id'], row['kickoff_at'])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        accepted.append(row)
    groups, scopes = {}, {}
    for row in accepted:
        scope_key = (row['experiment_id'], row['bookmaker'])
        scope = scopes.setdefault(scope_key, {'experiment_id': scope_key[0], 'bookmaker': scope_key[1],
                                             'paired_matches': 0, 'odds_band_groups': {}})
        scope['paired_matches'] += 1
        line = row['home_handicap']
        for index, venue in enumerate(('home', 'away')):
            side = ('flat_' + venue if line == 0 else
                    ('upper' if (line < 0) == (venue == 'home') else 'lower'))
            band = odds_band(row['odds'][index])
            key = (*scope_key, abs(line), side, venue, band)
            bucket = groups.setdefault(key, _bucket(
                experiment_id=scope_key[0], bookmaker=scope_key[1], abs_handicap=abs(line),
                side=side, venue=venue, odds_band=band, odds_band_label=_BAND_LABEL[band]))
            result = settle_asian_handicap(*row['final_score'], line, venue, row['odds'][index])
            _record(bucket, row, venue, result)
            total_key = (side, band)
            total = scope['odds_band_groups'].setdefault(total_key, _bucket(
                side=side, odds_band=band, odds_band_label=_BAND_LABEL[band],
                aggregation='all handicap sizes and venues within this experiment/bookmaker'))
            _record(total, row, venue, result)
    ordered_groups = [_finalize(bucket) for _, bucket in sorted(groups.items(), key=lambda item:
        (*item[0][:5], _BAND_ORDER[item[0][5]]))]
    book_summaries = []
    for _, scope in sorted(scopes.items()):
        scope['odds_band_groups'] = [_finalize(bucket) for _, bucket in sorted(
            scope['odds_band_groups'].items(), key=lambda item: (item[0][0], _BAND_ORDER[item[0][1]]))]
        book_summaries.append(scope)
    fixture_keys = {(row['match_id'], row['kickoff_at']) for row in accepted}
    return {
        'protocol_id': PROTOCOL_ID, 'evaluation_kind': 'descriptive_recorded_contracts',
        'odds_format': 'decimal', 'odds_bands': [dict(band) for band in ODDS_BANDS],
        'fixed_window': 'T-15m, 10 to 20 minutes before kickoff',
        'unique_fixtures': len(fixture_keys), 'accepted_pairs': len(accepted),
        'side_records': 2 * len(accepted), 'duplicate_pairs_excluded': duplicates,
        'rejected_pairs': rejected, 'rejection_counts': dict(Counter(item['reason'] for item in rejected)),
        'positive_return_denominator': 'all settled matches including pushes within each group',
        'profit_basis': 'descriptive one-unit settlement at each saved price, not a betting strategy',
        'groups': ordered_groups, 'book_summaries': book_summaries,
    }


def _text(value):
    return str(value).replace('|', '\\|').replace('\r', ' ').replace('\n', ' ')


def _counts(bucket):
    values = bucket['settlement_counts']
    return '/'.join(str(values.get(name, 0)) for name in CLASSES)


def _rate(value):
    return f'{value:.1%}' if value is not None else '—'


def render_price_venue_diagnostics(summary):
    """Chinese Markdown; descriptive totals remain distinct from model scores."""
    lines = ['### 盘口 × 水位 × 主客场（描述统计）', '',
             f"独立比赛：{summary.get('unique_fixtures', 0)}场；同公司同实验的配对记录："
             f"{summary.get('accepted_pairs', 0)}条。双方结算及不同实验/公司不能当作独立比赛。",
             '赔率为十进制；1.90–2.00对应香港净盈利水位0.90–1.00，1.75–1.85对应0.75–0.85。',
             '正收益率=(全赢+半赢)/全部结算（含走盘）；全赢率另列。以下不是模型命中成绩，也不证明优势。',
             '五档结算顺序：全赢/半赢/走盘/半输/全输；平手盘单列，不划为上盘或下盘。', '']
    if summary.get('duplicate_pairs_excluded') or summary.get('rejected_pairs'):
        lines += [f"排除重复配对{summary.get('duplicate_pairs_excluded', 0)}条，"
                  f"无效配对{len(summary.get('rejected_pairs', []))}条。", '']
    if not summary.get('groups'):
        return '\n'.join(lines + ['尚无符合固定T-15窗口且已配对赛果的盘口样本。', ''])
    for scope in summary['book_summaries']:
        lines += [f"**实验：{_text(scope['experiment_id'])}；公司：{_text(scope['bookmaker'])}；"
                  f"配对比赛：{scope['paired_matches']}场。**", '',
                  '水位概览汇总本实验、本公司的盘口大小及主客场；判断差异须再看下方交叉表。', '',
                  '| 侧别 | 十进制赔率 | 场数 | 五档结算 | 正收益率 | 全赢率 |',
                  '|---|---|---:|---|---:|---:|']
        lookup = {(group['side'], group['odds_band']): group for group in scope['odds_band_groups']}
        for side in ('upper', 'lower', 'flat_home', 'flat_away'):
            if not any(key[0] == side for key in lookup):
                continue
            for band in ODDS_BANDS:
                bucket = lookup.get((side, band['id']), _finalize(_bucket()))
                lines.append(f"| {_SIDE_LABEL[side]} | {band['label']} | {bucket['matches']} | "
                             f"{_counts(bucket)} | {_rate(bucket['positive_return_rate'])} | "
                             f"{_rate(bucket['full_win_rate'])} |")
        lines += ['', '| 让球大小（绝对值） | 侧别 | 所在主客场 | 十进制赔率 | 场数 | 五档结算 | 正收益率 | 全赢率 |',
                  '|---:|---|---|---|---:|---|---:|---:|']
        for group in summary['groups']:
            if (group['experiment_id'], group['bookmaker']) != (scope['experiment_id'], scope['bookmaker']):
                continue
            lines.append(f"| {group['abs_handicap']:g} | {_SIDE_LABEL[group['side']]} | "
                         f"{_VENUE_LABEL[group['venue']]} | {group['odds_band_label']} | {group['matches']} | "
                         f"{_counts(group)} | {_rate(group['positive_return_rate'])} | {_rate(group['full_win_rate'])} |")
        lines += ['']
    lines += ['小样本中的球队实力、盘口深度和主客场可能混在一起；需要同档球队及同期条件对照，不能凭水位汇总推断因果或未来命中率。', '']
    return '\n'.join(lines)
