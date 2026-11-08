"""Synthetic integration checks for immutable opening archives and T15 records."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'scripts'))

import f4_ah as ah
import f4_titan_research as titan
from test_f4_titan_features import archive, decision


NOW = datetime(2026, 11, 8, 4, tzinfo=timezone.utc)


def observation(number=42):
    row = {
        **decision(), 'match_id': f'espn:{number}', 'provider': 'espn',
        'source_event_id': str(number), 'home': 'Synthetic A', 'away': 'Synthetic B',
        'experiment_id': 'synthetic-titan-only', 'horizon': 'T-15m',
        'generated_at': '2026-11-07T14:45:01Z', 'status': 'scheduled',
        'bookmaker': 'crown', 'p_1x2': [.48, .27, .25],
        'provenance': {'source': 'https://example.invalid/synthetic'}, 'candidates': {},
        'quote': {'home_handicap': -.25, 'odds': [1.95, 1.91], 'bookmaker': 'crown',
                  'market': '90min_asian_handicap', 'captured_at': '2026-11-07T14:45:00Z',
                  'provider_event_id': str(number), 'url': 'https://example.invalid/synthetic',
                  'content_sha256': 'a' * 64},
    }
    return row


def save_case(root, number=42, *, completed=True, captured='2026-11-06T04:00:00Z', valid_market=True):
    row = observation(number)
    ah.write(root / 'data/f4/config.json', {'asian_handicap': {'experiment_id': row['experiment_id']}})
    if not valid_market:
        row['p_1x2'] = None
    ah.write(root / f'data/f4/ah/observations/2026-11-07/{number}.json', row, True)
    if completed:
        outcome = {key: row[key] for key in ('match_id', 'provider', 'source_event_id', 'league',
                                             'home_id', 'away_id', 'home', 'away', 'kickoff_at')}
        outcome.update(status='completed', observed_at='2026-11-07T18:00:00Z', home_score=2, away_score=1)
        ah.write(root / f'data/f4/outcomes/2026-11-07/{number}.json', outcome, True)
    payload = archive(captured)
    event = str(30000 + number)
    f = payload['fixture']
    f.update(canonical_match_id=row['match_id'], provider_event_id=event)
    h = payload['histories']['3']
    h['provider_event_id'] = event
    h['identity_check']['event_id'] = event
    h['source_url'] = f'https://vip.titan007.com/changeDetail/handicap.aspx?id={event}&companyID=3&l=0'
    h['source_fixture_identity'] = deepcopy(f)
    ah.write(root / f'data/f4/titan-history/observations/{event}/synthetic.json', payload, True)
    return row


def test_training_wrapper_uses_common_asof_cohort_and_preserves_original_contract(tmp_path):
    original = save_case(tmp_path)
    save_case(tmp_path, 43, captured='2026-11-07T14:46:00Z')
    save_case(tmp_path, 44, completed=False)
    save_case(tmp_path, 45, valid_market=False)
    status = titan.run(tmp_path, NOW)
    assert status['paired_t15_matches'] == 3
    assert status['eligible_completed_matches'] == 1
    assert status['pending_result_match_ids'] == ['espn:44']
    assert len(status['rejected']) == 2
    reasons = {item['match_id']: item['reason'] for item in status['rejected']}
    assert 'unavailable at decision time' in reasons['espn:43']
    assert reasons['espn:45'].startswith('base_market:')
    dataset = ah.read(tmp_path / status['dataset_path'])
    assert len(dataset['rows']) == 1
    row = dataset['rows'][0]
    assert row['titan_opening']['opening_home_water_raw'] == .9
    assert row['home_handicap'] == original['quote']['home_handicap']
    assert row['odds'] == original['quote']['odds']
    assert row['final_score'] == [2, 1]
    assert ah.read(tmp_path / 'data/f4/ah/observations/2026-11-07/42.json') == original
    report = ah.read(tmp_path / 'data/f4/ah/titan-research/latest.json')
    assert report['candidates']['M1']['counts'] == report['candidates']['M4']['counts']
    assert report['minimum_samples'] == {'train': 120, 'validation': 40, 'test': 40}


def test_monthly_trained_report_is_retained_while_availability_refreshes(tmp_path):
    save_case(tmp_path)
    location = tmp_path / 'data/f4/ah/titan-research'
    frozen = location / ('2026-11-' + titan.VERSION + '.json')
    report = {'status': 'trained_shadow', 'sentinel': 'immutable synthetic model',
              'candidates': {key: {'counts': {'train': 120, 'validation': 40, 'test': 40}}
                             for key in ('M1', 'M4')}}
    ah.write(frozen, report, True)
    before = frozen.read_bytes()
    with patch.object(titan, 'research', side_effect=AssertionError('frozen month was refit')):
        first = titan.run(tmp_path, NOW)
        save_case(tmp_path, 43, completed=False)
        second = titan.run(tmp_path, NOW)
    assert frozen.read_bytes() == before
    assert ah.read(location / 'latest.json') == report
    assert first['pending_result_match_ids'] == []
    assert second['pending_result_match_ids'] == ['espn:43']
    assert second['eligible_completed_matches'] == 1


def test_only_opening_candidate_success_freezes_month_and_experiments_are_filtered(tmp_path):
    save_case(tmp_path)
    other = save_case(tmp_path, 43)
    other['experiment_id'] = 'other-experiment'
    ah.write(tmp_path/'data/f4/ah/observations/2026-11-07/43.json', other)
    report = {'status': 'trained_shadow', 'candidates': {
        'M1': {'status': 'trained_shadow', 'counts': {}},
        'M4': {'status': 'fit_failed', 'counts': {}}}}
    with patch.object(titan, 'research', return_value=report) as fitted:
        result = titan.run(tmp_path, NOW)
    assert len(fitted.call_args.args[0]) == 1
    assert result['paired_t15_matches'] == 1
    assert result['model_status'] == 'fit_failed'
    assert not (tmp_path/'data/f4/ah/titan-research'/('2026-11-'+titan.VERSION+'.json')).exists()


def test_prospective_capture_attaches_opening_and_namespaced_models(tmp_path):
    # Install only the source archive. run_ah must create the actual snapshot.
    seeded = save_case(tmp_path, completed=False)
    (tmp_path / 'data/f4/ah/observations/2026-11-07/42.json').unlink()
    config = {'asian_handicap': {'enabled': True, 'experiment_id': seeded['experiment_id'],
              'research_only': True, 'automatic_promotion': False, 'automatic_wagering': False,
              'windows': {'180': 20, '60': 10, '15': 5}, 'max_capture_age_minutes': 5},
              'notifications': {'started_at': '2026-09-23T00:00:00Z'}}
    ah.write(tmp_path / 'data/f4/config.json', config)
    report = {'evaluated_at': '2026-11-06T12:00:00Z', 'candidates': {
        'M1': {'model': {'model_version': 'test-baseline'}},
        'M4': {'model': {'model_version': 'test-opening'}}}}
    ah.write(tmp_path / 'data/f4/ah/titan-research/latest.json', report)
    fixture = {key: seeded[key] for key in ('match_id', 'provider', 'source_event_id', 'league',
                                           'home_id', 'away_id', 'home', 'away', 'kickoff_at', 'status')}
    quote = deepcopy(seeded['quote'])
    quote['market_1x2'] = {**quote, 'market': '90min_1x2', 'odds': [2.0, 3.5, 4.0]}

    def source(root, fixtures, now, **kwargs):
        return {'markets': {fixture['match_id']: quote}, 'sources': [], 'problems': []}

    def predict(model, row):
        assert row['titan_opening']['canonical_match_id'] == row['match_id']
        return {'model_version': model['model_version'],
                'home': {'ev': .03, 'positive_return_probability': .6},
                'away': {'ev': -.05, 'positive_return_probability': .4}}

    with patch('f4_ah_research.predict', side_effect=predict) as prediction:
        result = ah.run_ah(tmp_path, [fixture], 'capture',
                           clock=lambda: ah.instant(seeded['observed_at']), source=source)
    assert result['new_valid'] == 1
    assert prediction.call_count == 2
    saved = ah.load_observations(tmp_path)[0]
    assert set(saved['candidates']) == {'TITAN_M1', 'M4'}
    assert saved['titan_opening_availability'] == 'available'
    assert saved['titan_opening']['archive_sha256']
    assert saved['generated_at'] == seeded['observed_at']
    assert saved['candidates']['M4']['hit_rate_direction'] == 'home'
    assert saved['candidate_weight'] == 0
