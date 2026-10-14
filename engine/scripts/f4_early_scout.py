"""Save the first price we actually see, separately from fixed capture windows.

This is not a bookmaker's certified opening line. All future matching uses the
same event, kickoff, company and source; no immutable T15 record is backfilled.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _instant(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('timezone required')
    return result.astimezone(timezone.utc)


def _stamp(value):
    return _instant(value).isoformat().replace('+00:00', 'Z')


def _read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def _write(path, value, *, immutable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if immutable:
        try:
            with path.open('x', encoding='utf-8') as stream:
                stream.write(body)
        except FileExistsError:
            if _read(path) != value:
                raise ValueError('first-observed price is immutable')
    else:
        path.write_text(body, encoding='utf-8')


def _key(row):
    return (row['experiment_id'], row['match_id'], _stamp(row['kickoff_at']))


def load_early_observations(root, *, issues=None):
    rows = []
    for path in (Path(root) / 'data/f4/ah/first-observed').glob('*/*.json'):
        try:
            row = _read(path)
            if not isinstance(row, dict) or not isinstance(row.get('quote'), dict) or not row['quote']:
                raise ValueError('missing early quote')
            for key in ('match_id', 'experiment_id', 'home_id', 'away_id'):
                if not isinstance(row.get(key), str) or not row[key].strip():
                    raise ValueError('missing early identity')
            if row['home_id'] == row['away_id'] or row.get('opening_certified') is not False:
                raise ValueError('invalid early identity or opening claim')
            if not (_instant(row['observed_at']) <= _instant(row['generated_at']) < _instant(row['kickoff_at'])):
                raise ValueError('invalid early observation timing')
            if row.get('horizon') != 'first-observed':
                raise ValueError('wrong early observation kind')
            rows.append(row)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if issues is not None:
                issues.append(path.name + ': ' + type(exc).__name__)
    return sorted(rows, key=lambda row: _instant(row['observed_at']))


def background_window_safe(fixtures, now, *, buffer_minutes=30):
    """No enrichment requests during or immediately before a capture window."""
    now = _instant(now)
    for fixture in fixtures:
        if fixture.get('status') != 'scheduled' or not fixture.get('kickoff_at'):
            continue
        try:
            minutes = (_instant(fixture['kickoff_at']) - now).total_seconds() / 60
        except (ValueError, TypeError):
            return False
        for target, tolerance in ((180, 20), (60, 10), (15, 5)):
            if target - tolerance <= minutes <= target + tolerance + buffer_minutes:
                return False
    return True


def scout_early_quotes(root, fixtures, now, *, source=None, clock=None, force=False):
    """One bounded daily scouting pass for verified matches over 200 min away."""
    root, now = Path(root), _instant(now)
    clock = clock or (lambda: datetime.now(timezone.utc))
    config = _read(root / 'data/f4/config.json', {})
    cfg = config.get('asian_handicap', {})
    if not cfg.get('enabled') or not config.get('enrichment', {}).get('first_observed_quotes', False):
        return {'status': 'disabled', 'new_records': 0}
    status_path = root / 'data/f4/ah/first-observed-status.json'
    previous = _read(status_path, {})
    if not force and previous.get('checked_at') and _instant(previous['checked_at']).date() == now.date():
        return {**previous, 'status': 'cached', 'new_records': 0}
    if not background_window_safe(fixtures, now):
        return {'status': 'deferred_capture_window', 'new_records': 0}
    old = load_early_observations(root)
    seen = {_key(row) for row in old}
    due = []
    for fixture in fixtures:
        if (fixture.get('provider') != 'espn' or fixture.get('league') != 'england-premier'
                or fixture.get('status') != 'scheduled' or not fixture.get('home_id')
                or not fixture.get('away_id') or not fixture.get('kickoff_at')):
            continue
        key = (cfg['experiment_id'], fixture['match_id'], _stamp(fixture['kickoff_at']))
        if key not in seen and _instant(fixture['kickoff_at']) - now > timedelta(minutes=200):
            due.append(fixture)
    due = sorted(due, key=lambda row: (row['kickoff_at'], row['match_id']))[:20]
    if not due:
        return {'status': 'no_unseen_early_fixtures', 'new_records': 0,
                'stored_first_observations': len(old)}
    from f4_ah import probability_1x2, validate_quote
    if source is None:
        # A single independent bulk request leaves the formal collector's
        # InferSports quota available for its three windows and retries.
        if 'hkjc' not in cfg.get('bookmaker_priority', ['crown']):
            return {'status': 'bulk_scout_source_not_allowed', 'new_records': 0}
        from f4_hkjc import attach_hkjc
        source = attach_hkjc
    bundle = source(root, due, now, probe=False)
    saved, missing = [], []
    for fixture in due:
        decision = _instant(clock())
        quote = bundle.get('markets', {}).get(fixture['match_id'])
        try:
            if _instant(fixture['kickoff_at']) - decision <= timedelta(minutes=200):
                raise ValueError('early scouting deadline passed')
            quote = validate_quote(quote, fixture, decision, cfg['max_capture_age_minutes'],
                                   cfg.get('bookmaker_priority', ['crown']))
            market = quote.get('market_1x2')
            probabilities = probability_1x2(market, decision, cfg['max_capture_age_minutes'], quote['bookmaker'])
            if probabilities is None:
                raise ValueError('same-company 1X2 unavailable')
        except (ValueError, KeyError, TypeError) as exc:
            missing.append({'match_id': fixture['match_id'], 'reason': str(exc)})
            continue
        generated = _instant(clock())
        if generated >= _instant(fixture['kickoff_at']):
            missing.append({'match_id': fixture['match_id'], 'reason': 'early record generation after kickoff'})
            continue
        row = {key: fixture.get(key) for key in ('match_id', 'provider', 'source_event_id',
               'home_id', 'away_id', 'home', 'away', 'kickoff_at', 'league')}
        row.update(schema_version=1, experiment_id=cfg['experiment_id'],
                   observed_at=_stamp(decision), generated_at=_stamp(generated),
                   horizon='first-observed', quote=quote, bookmaker=quote['bookmaker'],
                   p_1x2=probabilities, quote_1x2=market,
                   actual_minutes=(_instant(fixture['kickoff_at']) - decision).total_seconds() / 60,
                   selected=False, candidate_weight=0,
                   opening_certified=False,
                   provenance={'kind': 'prospective_first_observed_quote',
                               'note': 'First quote observed by this collector, not certified bookmaker opening',
                               'source': quote['url'], 'market': '90min_asian_handicap'})
        identity = hashlib.sha256(json.dumps(_key(row), ensure_ascii=False).encode()).hexdigest()
        row['observation_id'] = identity
        _write(root / 'data/f4/ah/first-observed' / row['kickoff_at'][:10] / (identity + '.json'), row, immutable=True)
        saved.append(fixture['match_id'])
    status = {'status': 'complete' if not missing else 'partial', 'checked_at': _stamp(clock()),
              'due_fixtures': len(due), 'new_records': len(saved), 'saved_matches': saved,
              'stored_first_observations': len(old) + len(saved), 'missing': missing,
              'provider_faults': bundle.get('provider_faults', {}),
              'source_problems': bundle.get('problems', []),
              'source_policy': 'independent_bulk_scout_no_formal_primary_quota',
              'opening_certified': False}
    _write(status_path, status)
    return status
