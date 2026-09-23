"""Delivery policy for the f4 research period.

This pure function plans a notice; it does not send mail or acknowledge delivery.
Persist ``state`` after every run, including failed deliveries, and pass the last
successful notify-ack file as ``last_ack``.  Only completed-month archives and
explicit critical incidents can produce notices.  A calendar date never enables
experimental directions.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


_NOT_FAULTS = {
    'no_matches', 'no_fixtures', 'no_due_fixtures', 'no_predictions',
    'no_selections', 'no_eligible_selections', 'candidate_untrained',
    'candidate_not_trained', 'model_untrained', 'model_not_trained',
    'candidate_not_ready', 'insufficient_samples',
}


def _instant(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('notification timestamps require a timezone')
    return parsed.astimezone(timezone.utc)


def _stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _local_zone(name):
    # Windows need not have an IANA timezone database for the configured HK zone.
    if name in ('Asia/Hong_Kong', 'Asia/Shanghai', 'UTC+08:00', '+08:00'):
        return timezone(timedelta(hours=8))
    return ZoneInfo(name)


def _incidents(health):
    result = {}
    for item in (health or {}).get('critical_incidents', []) or []:
        if not isinstance(item, dict):
            continue
        code, source = str(item.get('code') or '').strip(), str(item.get('source') or '').strip()
        if not code or not source or code.lower().replace('-', '_') in _NOT_FAULTS:
            continue
        key = _digest([code, source])[:24]
        result[key] = {'code': code, 'source': source, 'detail': str(item.get('detail') or '')}
    return result


def build_notice_policy(config, now, health, last_ack, monthly_report_available=None, *, state=None):
    """Return ``notify, reason, month, token, notice, state, critical_codes``.

    ``monthly_report_available`` maps YYYY-MM to the SHA256 of an existing,
    immutable report archive.  The caller must verify archive existence before
    passing the mapping.  Only the immediately preceding HK calendar month is
    newly scheduled, and never one before ``notifications.started_at``.

    Notice types are ``monthly_report`` and ``critical_incident``.  The latter
    deduplicates by source/code, not changing error text.  A recovered incident
    may start a new episode, subject to a default 24-hour recurrence cooldown.
    Uninterrupted incidents are sent once regardless of elapsed time.  Failed
    deliveries retain their token for retry; recovery itself is always silent.
    """
    now = _instant(now)
    settings = config.get('notifications') or {}
    local = now.astimezone(_local_zone(settings.get('timezone', 'Asia/Hong_Kong')))
    started = _instant(settings.get('started_at', '2026-09-23T00:00:00+08:00')).astimezone(local.tzinfo)
    previous_month = (local.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    report_month = previous_month if previous_month >= started.strftime('%Y-%m') else None
    cooldown = timedelta(hours=max(0, float(settings.get('critical_repeat_cooldown_hours', 24))))
    current = _incidents(health)
    saved = copy.deepcopy(state or {})
    saved.setdefault('schema_version', 1)
    saved.setdefault('incidents', {})
    saved.setdefault('pending', [])
    saved.setdefault('acknowledged_months', [])
    acknowledged = set(saved['acknowledged_months'])
    ack_token = last_ack.get('token') if isinstance(last_ack, dict) else last_ack

    def acknowledge(notice):
        if notice['type'] == 'monthly_report':
            acknowledged.add(notice['month'])
        else:
            for occurrence in notice['incidents']:
                record = saved['incidents'].get(occurrence['key'])
                if record:
                    record['acknowledged_episode'] = max(record.get('acknowledged_episode', 0), occurrence['episode'])
                    record['last_notified_at'] = _stamp(now)

    # The delivery ACK and policy state are deliberately separate.  Scheduling
    # a notice never counts as delivery, even if its data was already committed.
    remaining = []
    for notice in saved['pending']:
        if notice['token'] == ack_token:
            acknowledge(notice)
        else:
            remaining.append(notice)
    saved['pending'] = remaining
    if isinstance(last_ack, dict) and last_ack.get('type') == 'monthly_report' and last_ack.get('month'):
        acknowledged.add(last_ack['month'])

    for key, record in saved['incidents'].items():
        if key not in current and record.get('active'):
            record.update(active=False, recovered_at=_stamp(now))

    # Do not deliver a stale fault after it has recovered.  A mixed notice stays
    # intact while any original episode is still active, preserving its token.
    saved['pending'] = [notice for notice in saved['pending'] if notice['type'] != 'critical_incident'
                        or any(saved['incidents'].get(item['key'], {}).get('active')
                               and saved['incidents'][item['key']]['episode'] == item['episode']
                               for item in notice['incidents'])]
    queued_episodes = {(item['key'], item['episode']) for notice in saved['pending']
                       if notice['type'] == 'critical_incident' for item in notice['incidents']}
    new_faults = []
    for key, incident in sorted(current.items()):
        record = saved['incidents'].setdefault(key, {'episode': 0, 'active': False})
        if not record['active']:
            record.update(episode=record['episode'] + 1, active=True, first_seen_at=_stamp(now))
        record.update(incident, last_seen_at=_stamp(now))
        episode = record['episode']
        if (key, episode) in queued_episodes or record.get('acknowledged_episode') == episode:
            continue
        if record.get('last_notified_at') and now - _instant(record['last_notified_at']) < cooldown:
            continue
        new_faults.append({**incident, 'key': key, 'episode': episode,
                           'first_seen_at': record['first_seen_at']})

    def enqueue(notice, identity):
        notice['token'] = _digest({'policy': 'f4-research-notices-v1',
                                   'experiment': config.get('experiment_id'), **identity})
        if notice['token'] == ack_token:
            acknowledge(notice)
        else:
            saved['pending'].append(notice)

    if new_faults:
        enqueue({'type': 'critical_incident', 'month': local.strftime('%Y-%m'),
                 'created_at': _stamp(now), 'incidents': new_faults},
                {'type': 'critical_incident', 'occurrences': [
                    [item['key'], item['episode'], item['first_seen_at']] for item in new_faults]})

    available = monthly_report_available or {}
    sha = available.get(report_month) if report_month else None
    valid_sha = isinstance(sha, str) and len(sha) == 64 and all(c in '0123456789abcdefABCDEF' for c in sha)
    queued_months = {notice['month'] for notice in saved['pending'] if notice['type'] == 'monthly_report'}
    if report_month and valid_sha and report_month not in acknowledged and report_month not in queued_months:
        enqueue({'type': 'monthly_report', 'month': report_month, 'report_sha256': sha.lower(),
                 'created_at': _stamp(now), 'incidents': []},
                {'type': 'monthly_report', 'month': report_month, 'report_sha256': sha.lower()})

    saved['acknowledged_months'] = sorted(acknowledged)
    saved['updated_at'] = _stamp(now)
    # An urgent fault can pre-empt a waiting report, which remains queued.
    pending = sorted(saved['pending'], key=lambda item: (item['type'] != 'critical_incident', item['created_at']))
    notice = copy.deepcopy(pending[0]) if pending else None
    if notice:
        reason = notice['type']
    elif report_month and report_month not in acknowledged and not valid_sha:
        reason = 'monthly_report_not_available'
    else:
        reason = 'research_period_quiet'
    return {'notify': notice is not None, 'reason': reason,
            'month': notice['month'] if notice else report_month,
            'token': notice['token'] if notice else None, 'notice': notice, 'state': saved,
            'critical_codes': sorted({item['code'] for item in current.values()})}
