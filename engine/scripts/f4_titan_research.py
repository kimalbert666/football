"""Join available Titan openings to frozen T15 contracts and fit shadow models."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from f4_ah import digest, instant, load_observations, paired_rows, read, stamp, write, HK
from f4_ah_research import _validate_base, _features, research
from f4_titan_features import TitanOpeningIndex

VERSION = 'f4-ah-titan-opening-v1'


def run(root, now=None):
    now = now or datetime.now(timezone.utc)
    index = TitanOpeningIndex.from_root(root)
    experiment = read(root/'data/f4/config.json', {}).get('asian_handicap', {}).get('experiment_id')
    if not experiment:
        raise ValueError('configured AH experiment required')
    observations = [r for r in load_observations(root) if r.get('experiment_id') == experiment]
    paired, pairing_issues = paired_rows(root, observations, now)
    usable, rejected = [], []
    for row in paired:
        feature, reason = index.select(row)
        if feature is None:
            rejected.append({'match_id': row['match_id'], 'reason': reason})
            continue
        enriched = {**row, 'titan_opening': feature}
        try:
            _validate_base(enriched, settled=True, now=now)
            _features(enriched, 'M4')
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append({'match_id': row['match_id'], 'reason': 'base_market: '+str(exc)})
            continue
        usable.append(enriched)

    # Both models receive exactly this cohort. Source captures later than the
    # decision cannot enter the training set, even when the printed quote is old.
    destination = root/'data/f4/ah/titan-research'
    dataset_hash = digest(usable)
    dataset_path = destination/'datasets'/(dataset_hash+'.json')
    write(dataset_path, {'version': VERSION, 'rows_sha256': dataset_hash, 'rows': usable}, True)
    month = now.astimezone(HK).strftime('%Y-%m')
    frozen_path = destination/(month+'-'+VERSION+'.json')
    report = read(frozen_path)
    if report is None:
        report = research(usable, now=now, candidate_ids=('M1', 'M4'))
        report.update(evaluated_at=stamp(now), research_month=month,
                      dataset_path=dataset_path.relative_to(root).as_posix(),
                      dataset_sha256=dataset_hash, automatic_promotion=False)
        if report['candidates']['M4']['status'] == 'trained_shadow':
            write(frozen_path, report, True)
        write(destination/'latest.json', report)
    else:
        write(destination/'latest.json', report)

    # Report unfinished matches too, so waiting for a result is distinguishable
    # from missing or late initial-odds captures.
    seen, pending, prospective_reasons = set(), [], Counter()
    for row in observations:
        key = (row['match_id'], row['kickoff_at'], row['experiment_id'])
        if row.get('horizon') != 'T-15m' or not row.get('quote') or key in seen:
            continue
        seen.add(key)
        feature, reason = index.select(row)
        prospective_reasons['available_before_decision' if feature else reason] += 1
        if feature and row['match_id'] not in {r['match_id'] for r in paired}:
            pending.append(row['match_id'])
    status = {'version': VERSION, 'experiment_id': experiment, 'evaluated_at': stamp(now),
              'indexed_opening_archives': len(index.records), 'archive_rejections': index.rejected,
              'paired_t15_matches': len(paired), 'eligible_completed_matches': len(usable),
              'pending_result_match_ids': pending, 't15_opening_availability': dict(prospective_reasons),
              'pairing_issues': pairing_issues, 'rejected': rejected,
              'dataset_sha256': dataset_hash, 'dataset_path': dataset_path.relative_to(root).as_posix(),
              'model_status': report['candidates']['M4'].get('status', report['status']),
              'model_evaluated_at': report.get('evaluated_at'),
              'model_dataset_sha256': report.get('dataset_sha256'),
              'model_dataset_path': report.get('dataset_path'),
              'candidate_counts': {k: v['counts'] for k, v in report['candidates'].items()},
              'minimum_samples': {'train': 120, 'validation': 40, 'test': 40},
              'feature_contract': 'source_native_Crown_opening_covariates_available_before_T15',
              'quote_timezone_verified': False, 'decimal_conversion_performed': False,
              'automatic_promotion': False}
    write(destination/'availability.json', status)
    print({k: status[k] for k in ('paired_t15_matches', 'eligible_completed_matches', 'model_status')})
    return status


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    run(args.root)
