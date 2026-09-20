"""Offline exploratory replay of cached 90-minute football results and odds.

Never downloads, mutates source caches, or promotes a model. Historical quote
fields lack authenticated forecast timestamps: this is NOT prospective evidence.
Requires numpy/scipy, available in the football-runtime virtual environment.
"""
import argparse
import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, timezone
from itertools import groupby
from pathlib import Path

import numpy as np
import scipy
from scipy.optimize import minimize
from scipy.special import logsumexp

from evaluate import metrics, paired_bootstrap


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_cache(cache_dir, manifest):
    rows, exclusions, files, seen = [], [], [], {}
    for league in manifest['leagues']:
        for season in manifest['seasons']:
            path = Path(cache_dir) / f'odds_{league}_{season}.json'
            data = json.loads(path.read_text(encoding='utf-8'))
            if str(data.get('season')) != season:
                raise ValueError(f'Season mismatch: {path}')
            files.append({'path': str(path), 'sha256': digest(path),
                          'source': data.get('source'), 'fetchedAt': data.get('fetchedAt'),
                          'raw_rows': len(data['matches'])})
            for index, raw in enumerate(data['matches']):
                context = {'file': path.name, 'row': index}
                try:
                    day = datetime.strptime(raw['date'], '%d/%m/%Y').date().isoformat()
                    home, away = raw['home'], raw['away']
                    if not isinstance(home, str) or not isinstance(away, str) or not home or not away or home == away:
                        raise ValueError('invalid team identity')
                    goals = []
                    for key in ('fthg', 'ftag'):
                        v = raw[key]
                        if isinstance(v, bool) or str(v) != str(int(v)) or int(v) < 0:
                            raise ValueError('goals must be nonnegative integers')
                        goals.append(int(v))
                except (ValueError, TypeError, KeyError, OverflowError) as exc:
                    exclusions.append({**context, 'reason': str(exc), 'stage': 'result'})
                    continue
                match_id = '|'.join((league, day, home, away))
                if match_id in seen:
                    if raw != seen[match_id]:
                        raise ValueError(f'Conflicting duplicate: {match_id}')
                    exclusions.append({**context, 'match_id': match_id, 'reason': 'duplicate', 'stage': 'result'})
                    continue
                seen[match_id] = raw
                market = None
                try:
                    odds = [float(raw[manifest['odds_prefix'] + '_' + k]) for k in ('h', 'd', 'a')]
                    if any(not math.isfinite(o) or o <= 1 for o in odds):
                        raise ValueError('odds must be finite and >1')
                    inv = [1 / o for o in odds]
                    market = [p / sum(inv) for p in inv]
                except (ValueError, TypeError, KeyError, OverflowError) as exc:
                    exclusions.append({**context, 'match_id': match_id, 'reason': str(exc), 'stage': 'odds'})
                iso = date.fromisoformat(day).isocalendar()
                rows.append({'match_id': match_id, 'league': league, 'season': season,
                             'date': day, 'home': home, 'away': away, 'hg': goals[0], 'ag': goals[1],
                             'outcome': 'H' if goals[0] > goals[1] else 'D' if goals[0] == goals[1] else 'A',
                             'p_market': market, '_week': f'{iso.year}-W{iso.week:02d}',
                             'source_file': path.name})
    return sorted(rows, key=lambda r: (r['date'], r['match_id'])), exclusions, files


def poisson_probs(lh, la):
    upper = max(12, math.ceil(max(lh, la) + 8 * math.sqrt(max(lh, la))))
    def pmf(mean):
        values = [math.exp(-mean)]
        for k in range(1, upper + 1):
            values.append(values[-1] * mean / k)
        return np.array(values)
    matrix = np.outer(pmf(lh), pmf(la))
    matrix /= matrix.sum()
    return [float(np.tril(matrix, -1).sum()), float(np.trace(matrix)),
            float(np.triu(matrix, 1).sum())]


def construct_features(rows, manifest):
    """Predict entire calendar-day batch, then update results-derived states."""
    ratings = defaultdict(lambda: 1500.0)
    team_home, team_away = defaultdict(lambda: [0, 0, 0]), defaultdict(lambda: [0, 0, 0])
    league_totals = defaultdict(lambda: [0, 0, 0])
    current_season, output = {}, []
    prior = manifest['poisson_prior_games']
    for day, batch_iter in groupby(rows, key=lambda r: r['date']):
        batch = list(batch_iter)
        for league, season in sorted({(r['league'], r['season']) for r in batch}):
            if league in current_season and current_season[league] != season:
                for key in list(ratings):
                    if key[0] == league:
                        ratings[key] = 1500 + manifest['season_retention'] * (ratings[key] - 1500)
            current_season[league] = season
        deltas = defaultdict(float)
        for row in batch:
            league = row['league']
            hkey, akey = (league, row['home']), (league, row['away'])
            difference = (ratings[hkey] + manifest['elo_home_advantage'] - ratings[akey]) / 400
            expected = 1 / (1 + 10 ** (-difference))
            observed = 1 if row['outcome'] == 'H' else 0.5 if row['outcome'] == 'D' else 0
            change = manifest['elo_k'] * (observed - expected)
            deltas[hkey] += change
            deltas[akey] -= change
            n, hg, ag = league_totals[league]
            # Smoothing positive league means also avoids degenerate early zero-goal rates.
            home_rate = (hg + 1.5 * prior) / (n + prior)
            away_rate = (ag + 1.2 * prior) / (n + prior)
            hn, hgf, hga = team_home[hkey]
            an, agf, aga = team_away[akey]
            lh = ((hgf + prior * home_rate) / (hn + prior)
                  * (aga + prior * home_rate) / (an + prior) / home_rate)
            la = ((agf + prior * away_rate) / (an + prior)
                  * (hga + prior * away_rate) / (hn + prior) / away_rate)
            features = [difference, *[float(league == v) for v in manifest['leagues']]]
            output.append({**row, 'features': features, 'p_poisson': poisson_probs(lh, la),
                           'history_games_home_role': hn, 'history_games_away_role': an})
        for key, value in deltas.items():
            ratings[key] += value
        for row in batch:
            league = row['league']
            for stats, gf, ga in ((team_home[(league, row['home'])], row['hg'], row['ag']),
                                  (team_away[(league, row['away'])], row['ag'], row['hg']),
                                  (league_totals[league], row['hg'], row['ag'])):
                stats[0] += 1
                stats[1] += gf
                stats[2] += ga
    return output


def split_name(day, manifest):
    if day < manifest['warmup_end']:
        return 'warmup'
    if day < manifest['train_end']:
        return 'train'
    if day < manifest['validation_end']:
        return 'validation'
    if day < manifest['test_end']:
        return 'test'
    return 'external_partial' if day >= manifest['external_start'] else 'unused'


def fit_logistic(train, penalty, offset):
    x = np.array([r['features'] for r in train])
    y = np.array(['HDA'.index(r['outcome']) for r in train])
    baseline = np.log(np.maximum([r['p_market'] for r in train], 1e-15)) if offset else np.zeros((len(train), 3))
    target = np.eye(3)[y]
    shape = (x.shape[1], 3)
    def objective(flat):
        b = flat.reshape(shape)
        z = baseline + x @ b
        logp = z - logsumexp(z, axis=1, keepdims=True)
        loss = -logp[np.arange(len(y)), y].sum() + penalty * (b * b).sum() / 2
        grad = x.T @ (np.exp(logp) - target) + penalty * b
        return loss, grad.ravel()
    fitted = minimize(objective, np.zeros(np.prod(shape)), method='L-BFGS-B', jac=True,
                      options={'maxiter': 1000, 'ftol': 1e-12, 'gtol': 1e-7})
    if not fitted.success or not np.isfinite(fitted.x).all():
        raise ValueError(f'Logistic fit failed: {fitted.message}')
    return fitted.x.reshape(shape), {'penalty': penalty, 'market_offset': offset,
                                     'iterations': int(fitted.nit), 'objective': float(fitted.fun),
                                     'coefficients': fitted.x.reshape(shape).tolist()}


def predict_logistic(rows, coefficients, offset):
    x = np.array([r['features'] for r in rows])
    baseline = np.log(np.maximum([r['p_market'] for r in rows], 1e-15)) if offset else np.zeros((len(rows), 3))
    z = baseline + x @ coefficients
    return np.exp(z - logsumexp(z, axis=1, keepdims=True)).tolist()


def select_candidate(validation_metrics, relative_guard):
    base = validation_metrics['market']
    eligible = [name for name, values in validation_metrics.items()
                if all(values[key] <= base[key] * (1 + relative_guard)
                       for key in ('rps', 'brier', 'log_loss'))]
    return min(eligible, key=lambda name: (-validation_metrics[name]['accuracy'],
                                         validation_metrics[name]['log_loss'], name != 'market', name))


def benchmark(rows, manifest):
    featured = construct_features(rows, manifest)
    comparable = [dict(r, split=split_name(r['date'], manifest)) for r in featured if r['p_market'] is not None]
    train = [r for r in comparable if r['split'] == 'train']
    if not train or not any(r['split'] == 'validation' for r in comparable):
        raise ValueError('Nonempty training and validation periods required')
    for row in comparable:
        row['predictions'] = {'market': row['p_market'], 'poisson': row['p_poisson']}
    fitted_models = {}
    for offset in (False, True):
        for penalty in manifest['penalties']:
            name = f'{"market_offset" if offset else "elo_logistic"}_l2_{penalty}'
            coefficients, details = fit_logistic(train, penalty, offset)
            fitted_models[name] = details
            for row, p in zip(comparable, predict_logistic(comparable, coefficients, offset)):
                row['predictions'][name] = p
    models = list(comparable[0]['predictions'])
    split_metrics = {}
    for split in ('train', 'validation', 'test', 'external_partial'):
        subset = [r for r in comparable if r['split'] == split]
        split_metrics[split] = {name: metrics([dict(r, p=r['predictions'][name]) for r in subset], 'p') for name in models}
    selected = select_candidate(split_metrics['validation'], manifest['probability_guard_relative'])
    per_league, differences = {}, {}
    for split in ('test', 'external_partial'):
        subset = [r for r in comparable if r['split'] == split]
        per_league[split] = {}
        for league in manifest['leagues']:
            league_rows = [r for r in subset if r['league'] == league]
            per_league[split][league] = {name: metrics([dict(r, p=r['predictions'][name]) for r in league_rows], 'p') for name in models}
        differences[split] = {}
        for name in models:
            pairs = [dict(r, p_baseline=r['predictions']['market'], p_candidate=r['predictions'][name]) for r in subset]
            paired = paired_bootstrap(pairs, 2000, 42)
            paired['method'] = 'paired_source_calendar_week_cluster_percentile'
            if 'ci_unavailable_reason' in paired:
                paired['ci_unavailable_reason'] = 'Fewer than two source calendar weeks with matches'
            differences[split][name] = paired
    counts = {split: sum(r['split'] == split for r in comparable)
              for split in ('warmup', 'train', 'validation', 'test', 'external_partial', 'unused')}
    summary = {'historicalReplay': True, 'prospectiveEvidence': False, 'automaticPromotion': False,
               'aggregation': 'equal weight per match; pooled leagues; calendar-week clustered differences',
               'selected_on_validation': selected, 'split_counts': counts, 'metrics': split_metrics,
               'per_league': per_league, 'paired_differences_vs_market': differences,
               'fitted_models': fitted_models,
               'coverage': {'valid_result_rows': len(rows), 'comparable_quote_rows': len(comparable),
                            'market_top_probability_ties': sum(r['p_market'].count(max(r['p_market'])) > 1 for r in comparable),
                            'note': 'Denominator is cached rows, not independently verified complete fixture universe'},
               'limitations': manifest['limitations'] + [manifest['test_interpretation'],
                    'Initial goal priors: 1.5 home and 1.2 away; smoothed by manifest prior games; not tuned on test.',
                    'Market uses reported closing fields; this is a different information set than early prematch deployment.',
                    'Cross-season Poisson history retained; Elo deviation regresses by fixed manifest retention.',
                    'All model tables on test are diagnostic only; selecting their winner after inspection would require a new future test.']}
    return summary, comparable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error('Output directory already exists; preserve completed experiments')
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    boundaries = [manifest[k] for k in ('warmup_end', 'train_end', 'validation_end', 'test_end', 'external_start')]
    for boundary in boundaries:
        if date.fromisoformat(boundary).isoformat() != boundary:
            parser.error('Split boundaries must use YYYY-MM-DD dates')
    if boundaries != sorted(set(boundaries)):
        parser.error('Split boundaries must be strictly chronological')
    rows, exclusions, sources = load_cache(args.cache_dir, manifest)
    summary, predictions = benchmark(rows, manifest)
    summary.update({'experiment_id': manifest['experiment_id'], 'manifest_sha256': digest(args.manifest),
                    'code_sha256': digest(__file__),
                    'evaluator_sha256': digest(Path(__file__).with_name('evaluate.py')),
                    'dependencies': {'numpy': np.__version__, 'scipy': scipy.__version__},
                    'run_at': datetime.now(timezone.utc).isoformat(),
                    'sources': sources, 'exclusions': exclusions})
    args.output_dir.mkdir(parents=True)
    (args.output_dir / 'experiment.json').write_bytes(args.manifest.read_bytes())
    code_dir = args.output_dir / 'code'
    code_dir.mkdir()
    for source in (Path(__file__), Path(__file__).with_name('evaluate.py')):
        (code_dir / source.name).write_bytes(source.read_bytes())
    (args.output_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    with (args.output_dir / 'predictions.jsonl').open('w', encoding='utf-8') as fh:
        for row in predictions:
            row = {k: v for k, v in row.items() if not k.startswith('_')}
            row.update({'historicalReplay': True, 'prospectiveEvidence': False,
                        'experiment_id': manifest['experiment_id']})
            fh.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
    print(json.dumps({'output': str(args.output_dir), 'selected': summary['selected_on_validation'],
                      'split_counts': summary['split_counts']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
