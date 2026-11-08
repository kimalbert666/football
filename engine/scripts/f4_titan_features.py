"""As-of research covariates from immutable, source-reconciled Titan openings.

The daily archive's generic training_eligible flag remains false. This adapter
applies its own narrowly scoped research eligibility policy to the displayed
opening handicap and *native* water values. Availability comes from the actual
archive capture, so no conversion of the provider's unverified quote timezone
or odds format is needed. Settlement always uses the parent's decision quote.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


POLICY = 'titan-native-opening-asof-v1'
ACCEPTED = {'displayed_history_reconciled', 'displayed_history_reconciled_with_exclusions'}
FEATURE_KEYS = ('opening_home_handicap', 'opening_home_water_raw', 'opening_away_water_raw')


def _time(value):
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError('explicit timezone-aware timestamp required')
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.utcoffset() is None:
        raise ValueError('timezone required')
    return moment.astimezone(timezone.utc)


def _number(value, *, water=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('opening numeric field invalid')
    number = float(value)
    if water and not 0 < number < 20:
        raise ValueError('opening native water invalid')
    if not water and not math.isclose(number * 4, round(number * 4), abs_tol=1e-9, rel_tol=0):
        raise ValueError('opening handicap is not a quarter-goal value')
    return number


def _raw_water(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('opening native water invalid')
    return _number(float(value), water=True)


def _source(url, event):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'vip.titan007.com'
            or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment
            or parsed.path.lower() != '/changedetail/handicap.aspx'
            or sorted((k.lower(), v) for k, v in parse_qsl(parsed.query, keep_blank_values=True))
            != sorted([('id', event), ('companyid', '3'), ('l', '0')])):
        raise ValueError('opening source market or identity invalid')


def validate_feature_record(feature, row):
    """Validate an extracted record against the actual prediction cutoff row."""
    if not isinstance(feature, dict) or feature.get('eligibility_policy') != POLICY:
        raise ValueError('Titan opening feature missing or unsupported')
    if feature.get('water_scale') != 'provider_native_unconverted':
        raise ValueError('Titan opening native scale declaration required')
    if row.get('league', 'england-premier') != 'england-premier':
        raise ValueError('Titan opening league mismatch')
    for parent, child in (('match_id', 'canonical_match_id'), ('home_id', 'home_id'), ('away_id', 'away_id')):
        if not row.get(parent) or feature.get(child) != row[parent]:
            raise ValueError('Titan opening fixture identity mismatch')
    kickoff, decision, capture = _time(row['kickoff_at']), _time(row['observed_at']), _time(feature['captured_at_utc'])
    if _time(feature['kickoff_at']) != kickoff or not capture <= decision < kickoff:
        raise ValueError('Titan opening unavailable at decision time')
    if feature.get('company_id') != '3' or not str(feature.get('event_id', '')).isdigit():
        raise ValueError('Titan opening company invalid')
    _source(feature.get('source', ''), feature['event_id'])
    path = feature.get('archive_path', '')
    if (not isinstance(path, str) or not path.startswith('data/f4/titan-history/observations/'+feature['event_id']+'/')
            or '..' in Path(path).parts):
        raise ValueError('Titan opening archive path invalid')
    digest = feature.get('archive_sha256', '')
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Titan opening archive digest missing')
    return {**feature, **{key: _number(feature[key], water=key.endswith('water_raw')) for key in FEATURE_KEYS}}


def _extract(payload, path, content):
    if payload.get('status') not in ACCEPTED:
        raise ValueError('opening archive not reconciled')
    fixture, histories = payload['fixture'], payload['histories']
    history = histories['3']
    event = fixture['provider_event_id']
    capture = _time(payload['captured_at_utc'])
    if (history.get('company_id') != '3' or history.get('provider_event_id') != event
            or history.get('fixture_identity_verified') is not True
            or history.get('provider_opening_reconciled') is not True
            or history.get('accepted_for_research_archive') is not True
            or history.get('projection_truncated') or history.get('pagination_controls')):
        raise ValueError('opening history identity or coverage invalid')
    identity = history.get('identity_check') or {}
    if (identity.get('identity_verified') is not True or identity.get('event_id') != event
            or identity.get('company_id') != '3' or identity.get('issues')):
        raise ValueError('opening history identity failed')
    if history.get('fixture_kickoff_display') != fixture.get('kickoff_display'):
        raise ValueError('opening displayed kickoff mismatch')
    detail_fixture = history.get('source_fixture_identity') or {}
    for key in ('provider_event_id', 'canonical_match_id', 'home_id', 'away_id',
                'kickoff_display', 'independent_fixture_kickoff_at'):
        if not fixture.get(key) or detail_fixture.get(key) != fixture[key]:
            raise ValueError('opening detail fixture metadata mismatch')
    if _time(history['captured_at_utc']) > capture:
        raise ValueError('opening archive capture inconsistent')
    mapping = fixture['mapping_source_evidence']
    mapping_url = urlsplit(mapping['url'])
    if (mapping.get('status') != 'ok' or mapping.get('http_status') != 200
            or mapping_url.scheme != 'https' or mapping_url.hostname != 'bf.titan007.com'
            or not mapping_url.path.startswith('/football/Next_')
            or _time(mapping['captured_at_utc']) > capture):
        raise ValueError('opening independent mapping evidence invalid')
    if _time(fixture['independent_fixture_last_seen_at']) > capture:
        raise ValueError('opening independent fixture availability inconsistent')
    opening, earliest = history['source_reported_opening'], history['earliest_visible_quote']
    first = earliest.get('quote') or {}
    if (earliest.get('state') != 'unique_at_displayed_minute'
            or earliest.get('displayed_datetime') != opening.get('displayed_full_datetime')
            or first.get('home_handicap') != opening.get('home_handicap')
            or _raw_water(first.get('home_water_raw')) != _raw_water(opening.get('home_water_raw'))
            or _raw_water(first.get('away_water_raw')) != _raw_water(opening.get('away_water_raw'))):
        raise ValueError('opening does not reconcile with first historical quote')
    feature = {
        'eligibility_policy': POLICY, 'canonical_match_id': fixture['canonical_match_id'],
        'home_id': fixture['home_id'], 'away_id': fixture['away_id'],
        'kickoff_at': fixture['independent_fixture_kickoff_at'],
        'captured_at_utc': payload['captured_at_utc'], 'event_id': event, 'company_id': '3',
        'source': history['source_url'], 'archive_path': path,
        'archive_sha256': hashlib.sha256(content).hexdigest(),
        'opening_home_handicap': _number(opening['home_handicap']),
        'opening_home_water_raw': _raw_water(opening['home_water_raw']),
        'opening_away_water_raw': _raw_water(opening['away_water_raw']),
        'water_scale': 'provider_native_unconverted',
        'opening_displayed_datetime': opening['displayed_full_datetime'],
        'quote_time_utc': None, 'bookmaker_original_opening_certified': False,
    }
    _source(feature['source'], event)
    # Validate cutoff separately when selecting; retrospective files remain
    # indexed so select can explain why they were unavailable at that cutoff.
    _time(feature['kickoff_at'])
    return feature


class TitanOpeningIndex:
    def __init__(self, records=(), rejected=()):
        self.records = sorted(records, key=lambda f: (_time(f['captured_at_utc']), f['archive_path']))
        self.rejected = list(rejected)

    @classmethod
    def from_root(cls, root):
        root = Path(root)
        records, rejected = [], []
        for path in sorted((root / 'data/f4/titan-history/observations').glob('*/*.json')):
            relative = path.relative_to(root).as_posix()
            try:
                content = path.read_bytes()
                payload = json.loads(content)
                records.append(_extract(payload, relative, content))
            except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
                rejected.append({'archive_path': relative, 'reason': str(exc)})
        return cls(records, rejected)

    def select(self, row):
        """Select the first available opening and reject conflicting as-of claims."""
        matching = [f for f in self.records if f['canonical_match_id'] == row.get('match_id')]
        if not matching:
            return None, 'no reconciled Titan opening archive for fixture'
        valid, rejected = [], []
        for feature in matching:
            try:
                valid.append(validate_feature_record(feature, row))
            except (KeyError, ValueError, TypeError, OverflowError) as exc:
                rejected.append(str(exc))
        if not valid:
            return None, '; '.join(sorted(set(rejected)))
        claims = {(f['event_id'], f['company_id'], *(f[key] for key in FEATURE_KEYS),
                   f['opening_displayed_datetime']) for f in valid}
        if len(claims) != 1:
            return None, 'conflicting Titan opening claims available at decision time'
        return valid[0], 'available'
