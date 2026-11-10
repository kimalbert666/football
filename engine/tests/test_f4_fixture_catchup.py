"""Known fixtures without a saved prediction still receive result recovery."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import f4_cloud as cloud
from f4_ledger import load_latest_outcomes, save_outcome


NOW = datetime(2026, 11, 10, 1, tzinfo=timezone.utc)


def fixture(number, kickoff=None, **changes):
    row = {'match_id': 'espn:'+str(number), 'source_event_id': str(number), 'provider': 'espn',
           'league': 'england-premier', 'home_id': 'arsenal', 'away_id': 'chelsea',
           'home': 'Synthetic A', 'away': 'Synthetic B', 'status': 'scheduled', 'score': None,
           'kickoff_at': cloud.stamp(kickoff or NOW-timedelta(days=2)), 'problems': []}
    return {**row, **changes}


def configure(root):
    cloud.write_json(root/'data/f4/config.json', {
        'mode': 'shadow', 'candidate_weight': 0, 'experiment_id': 'synthetic-catchup',
        'scope': 'league', 'league': 'england-premier', 'tracked_teams_path': 'teams.json',
        'horizons_minutes': [180, 60], 'horizon_tolerance_minutes': 20, 'max_capture_age_minutes': 15,
    })
    cloud.write_json(root/'teams.json', {'teams': []})


def test_missed_t15_cached_fixture_is_recovered_and_result_saved(tmp_path):
    if sys.platform == 'win32':
        tmp_path = Path('\\\\?\\' + str(tmp_path))
    configure(tmp_path)
    cached = fixture(42)
    cloud.write_json(tmp_path/'data/f4/fixtures/42.json', cached)
    seen = []

    def collector(root, now, **options):
        seen.extend(options['pending'])
        completed = {**cached, 'status': 'completed', 'score': [2, 1]}
        return {'captured_at': cloud.stamp(NOW), 'fixtures': [completed], 'sources': [],
                'coverage': {'complete': True}, 'problems': []}

    report = cloud.run(tmp_path, 'daily', collector=collector, clock=lambda: NOW)
    assert [r['match_id'] for r in seen] == ['espn:42']
    assert report['new_outcomes'] == 1
    outcome = load_latest_outcomes(tmp_path)['espn:42']
    assert outcome['home_score'] == 2 and outcome['away_score'] == 1
    assert cloud.load_predictions(tmp_path) == []


def test_recent_pending_first_and_finished_future_other_league_and_invalid_are_skipped(tmp_path):
    if sys.platform == 'win32':
        tmp_path = Path('\\\\?\\' + str(tmp_path))
    configure(tmp_path)
    completed = fixture(10, status='completed', score=[1, 0])
    save_outcome(tmp_path, {**completed, 'home_score': 1, 'away_score': 0}, cloud.stamp(NOW))
    rows = [fixture(1, NOW-timedelta(days=3)), fixture(2, NOW-timedelta(days=2)),
            completed, fixture(11, NOW+timedelta(hours=1)), fixture(12, league='spain-la-liga'),
            fixture(13, match_id='espn:999'), fixture(14, provider='other'),
            fixture(15, home_id=None), fixture(16, kickoff_at='broken')]
    for number, row in enumerate(rows):
        cloud.write_json(tmp_path/f'data/f4/fixtures/{number}.json', row)
    seen = []

    def collector(root, now, **options):
        seen.extend(options['pending'])
        return {'captured_at': cloud.stamp(NOW), 'fixtures': [], 'sources': [],
                'coverage': {'complete': True}, 'problems': []}

    cloud.run(tmp_path, 'daily', collector=collector, clock=lambda: NOW)
    assert [r['match_id'] for r in seen] == ['espn:2', 'espn:1']
