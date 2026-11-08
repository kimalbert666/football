"""Opt-in opening experiment, original model compatibility and cutoff guards."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))

import f4_ah_research as ah


BASE = datetime(2025, 1, 1, 15, tzinfo=timezone.utc)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def fixture(index=0):
    kickoff = BASE + timedelta(days=index)
    event = str(90000 + index)
    row = {
        'match_id': 'espn:' + event, 'home_id': 'home-team', 'away_id': 'away-team',
        'league': 'england-premier', 'horizon': 'T-15m',
        'kickoff_at': kickoff.isoformat(),
        'observed_at': (kickoff - timedelta(minutes=15)).isoformat(),
        'settled_at': (kickoff + timedelta(hours=3)).isoformat(),
        'provenance': {'source': 'synthetic-test-only'},
        'home_handicap': [-.25, .25, -.75, .75][index % 4],
        'odds': {'home': 1.9, 'away': 2.0}, 'bookmaker': 'hkjc',
        'p_1x2': [.48, .27, .25],
        'final_score': [index % 4, (index // 3) % 3],
    }
    row['titan_opening'] = {
        'eligibility_policy': 'titan-native-opening-asof-v1',
        'canonical_match_id': row['match_id'], 'home_id': row['home_id'], 'away_id': row['away_id'],
        'kickoff_at': row['kickoff_at'], 'captured_at_utc': (kickoff-timedelta(days=2)).isoformat(),
        'company_id': '3', 'event_id': event,
        'source': f'https://vip.titan007.com/changeDetail/handicap.aspx?id={event}&companyID=3&l=0',
        'archive_path': f'data/f4/titan-history/observations/{event}/synthetic-test-only.json',
        'archive_sha256': 'a' * 64, 'water_scale': 'provider_native_unconverted',
        'opening_home_handicap': row['home_handicap'] + .25,
        'opening_home_water_raw': .85 + (index % 3) / 100,
        'opening_away_water_raw': 1.03 - (index % 3) / 100,
    }
    return row


def test_existing_candidate_contract_and_features_unchanged():
    assert ah.CANDIDATES == ('M1', 'M2', 'M3')
    assert ah.SCHEMA_VERSION == 'f4-ah-research-v2'
    row = fixture()
    before = ah._features(row, 'M1')
    changed = copy.deepcopy(row)
    changed['titan_opening'] = {'captured_at_utc': row['kickoff_at']}
    assert ah._features(changed, 'M1') == before
    default = ah.research([row], now=NOW)
    assert tuple(default['candidates']) == ah.CANDIDATES
    assert default['schema_version'] == ah.SCHEMA_VERSION


def test_opening_is_native_covariate_and_accepts_current_different_book():
    row = fixture()
    values = ah._features(row, 'M4')
    assert len(values) == len(ah.FEATURES['M4'])
    assert values[:len(ah.BASE_FEATURES)] == ah._features(row, 'M1')
    assert values[-4:] == [0, 0, .85, 1.03]
    assert row['odds'] == {'home': 1.9, 'away': 2.0}


@pytest.mark.parametrize('change', [
    {'captured_at_utc': BASE.isoformat()},
    {'canonical_match_id': 'espn:other'}, {'home_id': 'away-team'},
    {'company_id': '48'}, {'opening_home_water_raw': float('nan')},
    {'opening_home_water_raw': True}, {'opening_home_handicap': -.3},
    {'water_scale': 'decimal'},
])
def test_future_conflicting_or_malformed_opening_cannot_enter_features(change):
    row = fixture()
    row['titan_opening'].update(change)
    with pytest.raises(ValueError):
        ah._features(row, 'M4')


@pytest.fixture(scope='module')
def fitted():
    rows = [fixture(i) for i in range(100)]
    report = ah.research(rows, now=NOW, candidate_ids=('M1', 'M4'),
                         min_train=30, min_validation=10, min_test=10)
    return rows, report


def test_opening_candidate_real_fit_serializes_and_compares_same_test_matches(fitted):
    rows, report = fitted
    assert report['schema_version'] == ah.TITAN_SCHEMA_VERSION
    assert report['status'] == 'trained_shadow'
    assert tuple(report['candidates']) == ('M1', 'M4')
    assert json.loads(json.dumps(report, allow_nan=False)) == report
    for candidate in report['candidates'].values():
        assert candidate['status'] == 'trained_shadow', candidate['reason']
        assert candidate['model']['schema_version'] == ah.TITAN_SCHEMA_VERSION
        assert candidate['model']['weight'] == 0
    common = report['common_test_comparisons']['M1+M4']
    assert common['count'] == report['split']['test']['count']
    assert common['common_test_coverage'] == 1
    train_ids = report['split']['train']['match_ids']
    matrix = np.array([ah._features(r, 'M4') for r in rows if r['match_id'] in train_ids])
    assert report['candidates']['M4']['model']['mean'] == pytest.approx(matrix.mean(axis=0))


def test_final_test_labels_cannot_change_fit_or_selection(fitted):
    original, report = fitted
    rows = copy.deepcopy(original)
    for row in rows:
        if row['match_id'] in report['split']['test']['match_ids']:
            row['final_score'] = [12, 0]
    updated = ah.research(rows, now=NOW, candidate_ids=('M1', 'M4'),
                          min_train=30, min_validation=10, min_test=10)
    for name in ('M1', 'M4'):
        assert updated['candidates'][name]['model'] == report['candidates'][name]['model']
        assert updated['candidates'][name]['validation'] == report['candidates'][name]['validation']


def test_missing_opening_excludes_candidate_but_retains_matched_market_baseline():
    row = fixture()
    row.pop('titan_opening')
    report = ah.research([row], now=NOW, candidate_ids=('M1', 'M4'))
    assert sum(report['candidates']['M1']['counts'].values()) == 1
    assert sum(report['candidates']['M4']['counts'].values()) == 0
    assert report['candidates']['M4']['model'] is None


def test_new_models_predict_at_future_cutoff_and_reject_schema_confusion(fitted):
    _, report = fitted
    row = fixture(400)
    for name in ('M1', 'M4'):
        model = report['candidates'][name]['model']
        prediction = ah.predict(model, row)
        assert sum(prediction['home']['probabilities'].values()) == pytest.approx(1)
    wrong = {**report['candidates']['M4']['model'], 'schema_version': ah.SCHEMA_VERSION}
    with pytest.raises(ValueError, match='unsupported model schema'):
        ah.predict(wrong, row)


def test_experiment_requires_predeclared_unique_candidate_pair():
    for names in ((), ('M4',), ('M1', 'M4', 'M2'), ('M1', 'M1'), ('unknown',)):
        with pytest.raises(ValueError):
            ah.research([], now=NOW, candidate_ids=names)
