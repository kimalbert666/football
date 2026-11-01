"""Offline, conservative archive of Titan's displayed handicap-history rows.

No networking or production writes. This module intentionally cannot certify
complete history or manufacture prospective observations from retrospective data.
Input HTML should be the collector's visible-DOM projection; raw HTML visibility
is only approximated by the diagnostic parser and is not a completeness proof.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from titan_cloud_probe import Document, FULL_DATE, SHORT_DATE, clean, direct_cells, profit_odds
from titan_general import parse_home_handicap as home_handicap

PREMATCH = {"早", "即"}
INPLAY = {"滚", "滾"}
SUSPENDED = {"封", "封盘", "封盤", "停", "暂停", "暫停"}


def _full_datetime(value):
    match = FULL_DATE.fullmatch(clean(value or ""))
    if not match:
        return None
    try:
        return datetime(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _change_time(value, opening, kickoff):
    full = _full_datetime(value)
    if full:
        return full, "explicit_full_year", None
    short = SHORT_DATE.fullmatch(clean(value or ""))
    if not short:
        return None, None, "invalid_or_missing_change_time"
    if opening is None:
        return None, None, "short_date_requires_full_opening_anchor"
    candidates = []
    for year in range(opening.year, kickoff.year + 1):
        try:
            candidate = datetime(year, *(int(part) for part in short.groups()))
        except ValueError:
            continue
        # A historical page can contain in-play rows too. Retain their year
        # when uniquely anchored, but never turn them into prematch quotations.
        if opening <= candidate <= kickoff + timedelta(days=1):
            candidates.append(candidate)
    if len(candidates) != 1:
        return None, None, "short_date_year_ambiguous_or_outside_anchor"
    return candidates[0], "unique_between_full_opening_anchor_and_fixture_plus_1d", None


def _endpoint(rows, *, cutoff=None, earliest=False, exclusive_cutoff=False):
    # An undated non-live event could be newer than the last dated quotation.
    # Preserve its existence rather than silently selecting a stale endpoint.
    undated = [r for r in rows if r["kind"] != "header"
               and r["raw_status"] not in INPLAY and not r["displayed_datetime"]]
    if undated:
        return {"state": "unresolved_event_time",
                "row_indices": [r["source_row_index"] for r in undated], "quote": None}
    candidates = [r for r in rows if r["displayed_datetime"]
                  and r["raw_status"] not in INPLAY
                  and (cutoff is None or
                       (r["displayed_datetime"] < cutoff if exclusive_cutoff
                        else r["displayed_datetime"] <= cutoff))]
    if not candidates:
        return {"state": "missing", "row_indices": [], "quote": None}
    instant = (min if earliest else max)(r["displayed_datetime"] for r in candidates)
    group = [r for r in candidates if r["displayed_datetime"] == instant]
    if cutoff is not None and not exclusive_cutoff and instant == cutoff:
        return {"state": "ambiguous_cutoff_minute", "displayed_datetime": instant,
                "row_indices": [r["source_row_index"] for r in group], "quote": None}
    # Never resolve same-minute ordering from a sort's incidental stability.
    if len(group) != 1:
        return {"state": "ambiguous_same_minute", "displayed_datetime": instant,
                "row_indices": [r["source_row_index"] for r in group], "quote": None}
    selected = group[0]
    if selected["issues"]:
        return {"state": "unresolved_event", "displayed_datetime": instant,
                "row_indices": [selected["source_row_index"]], "quote": None}
    if selected["kind"] == "suspended":
        return {"state": "suspended", "displayed_datetime": instant,
                "row_indices": [selected["source_row_index"]], "quote": None}
    return {"state": "unique_at_displayed_minute", "displayed_datetime": instant,
            "row_indices": [selected["source_row_index"]], "quote": selected["quote"]}


def archive_history(html, *, kickoff_display, captured_at_utc, source_url,
                    opening_display=None, projection_truncated=False,
                    fixture_identity_verified=False, odds_format_verified=False):
    """Preserve displayed rows, classify conservatively, and expose ambiguity.

    `opening_display` is a separately reported full timestamp, used only as an
    explicit year anchor. This function does not itself reconcile opening odds.
    No timezone argument is accepted: until separately implemented and audited,
    all quote datetimes remain source-display naive times and UTC stays null.
    Source rows are never deduplicated, including identical rows.
    """
    kickoff = _full_datetime(kickoff_display)
    opening = _full_datetime(opening_display)
    if kickoff is None:
        raise ValueError("full fixture display datetime required")
    if opening_display and (opening is None or opening >= kickoff):
        raise ValueError("opening anchor must be a full datetime before kickoff")
    captured = datetime.fromisoformat(captured_at_utc.replace("Z", "+00:00"))
    if captured.tzinfo is None or captured.utcoffset() != timedelta(0):
        raise ValueError("actual capture timestamp must be explicit UTC")
    document = Document(html)
    tables = list(document.root.descendants("table"))
    if len(tables) != 1:
        raise ValueError("one isolated history-table projection required")
    rows = []
    for ordinal, node in enumerate(tables[0].descendants("tr")):
        cells = direct_cells(node)
        if not cells or not node.visible():
            continue
        raw_cells, slots, row_issues = [], [], []
        for cell in cells:
            try:
                span = int(cell.attrs.get("colspan", "1"))
            except (TypeError, ValueError):
                span = 0
            text = clean(cell.text())
            raw_cells.append({"text": text, "colspan": span,
                              "inline_visible": cell.visible()})
            if not 1 <= span <= 7:
                row_issues.append("invalid_colspan")
            else:
                slots += [text] + [None] * (span - 1)
        record = {"source_row_index": ordinal, "raw_cells": raw_cells,
                  "raw_status": None, "raw_change_time": None,
                  "displayed_datetime": None, "quote_time_utc": None,
                  "timestamp_precision": "minute", "year_basis": None,
                  "kind": "unrecognized_row", "prematch_quote": False,
                  "quote": None, "issues": row_issues}
        if len(slots) != 7:
            record["issues"].append("unexpected_column_count")
            rows.append(record)
            continue
        clock, score, home, handicap, away, raw_time, status = slots
        record.update(raw_status=status, raw_change_time=raw_time)
        if raw_time in {"变化时间", "變化時間"} and status in {"状态", "狀態"}:
            record["kind"] = "header"
            rows.append(record)
            continue
        moment, basis, time_issue = _change_time(raw_time, opening, kickoff)
        if moment:
            record["displayed_datetime"] = moment.strftime("%Y-%m-%d %H:%M")
            record["year_basis"] = basis
        if time_issue:
            record["issues"].append(time_issue)
        if any(c["inline_visible"] is False for c in raw_cells):
            record["issues"].append("hidden_candidate_cell")
        if status not in PREMATCH | INPLAY | SUSPENDED:
            record["issues"].append("unknown_status")
        if status in SUSPENDED or home in SUSPENDED or handicap in SUSPENDED or away in SUSPENDED:
            record["kind"] = "suspended"
        else:
            hp, ap = profit_odds(home or ""), profit_odds(away or "")
            ah = home_handicap(handicap or "")
            if hp is None or ap is None or ah is None:
                record["issues"].append("unparseable_quote")
            else:
                record["kind"] = "quote"
                record["quote"] = {"home_handicap": ah, "home_water_raw": home,
                                   "away_water_raw": away, "handicap_raw": handicap,
                                   "home_hk_water": hp if odds_format_verified else None,
                                   "away_hk_water": ap if odds_format_verified else None,
                                   "home_decimal_odds": round(hp + 1, 3) if odds_format_verified else None,
                                   "away_decimal_odds": round(ap + 1, 3) if odds_format_verified else None}
        if status in SUSPENDED and (clock or score):
            record["issues"].append("suspended_status_has_clock_or_score")
        if status in PREMATCH:
            if clock or score:
                record["issues"].append("prematch_status_has_clock_or_score")
            if moment and moment >= kickoff:
                record["issues"].append("prematch_not_before_kickoff")
            if moment and opening and moment < opening:
                record["issues"].append("quote_precedes_opening_anchor")
            record["prematch_quote"] = record["kind"] == "quote" and not record["issues"]
        rows.append(record)
    visible_controls = []
    for node in document.root.descendants():
        if node.tag in {"a", "button", "option"} and node.visible():
            label = clean(node.text())
            if re.search(r"下一页|下頁|下一頁|加载更多|載入更多|load more|next", label, re.I):
                visible_controls.append(label[:120])
    data_rows = [r for r in rows if r["kind"] != "header"]
    issues = Counter(i for r in data_rows for i in r["issues"])
    time_groups = Counter(r["displayed_datetime"] for r in data_rows if r["displayed_datetime"])
    raw_digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cutoff = (kickoff - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")
    return {"schema_version": "titan-displayed-history-archive-v1",
            "source_url": source_url, "captured_at_utc": captured.astimezone(timezone.utc).isoformat(),
            "fixture_kickoff_display": kickoff_display, "year_anchor_display": opening_display,
            "fixture_identity_verified": bool(fixture_identity_verified),
            "odds_format_verified": bool(odds_format_verified),
            "timezone_verified": False, "provider_opening_reconciled": False,
            "formal_observation": False, "training_eligible": False,
            "displayed_history_coverage": "partial" if projection_truncated or visible_controls else "unknown",
            "bookmaker_every_change_certified": False,
            "parse_ok": bool(data_rows) and not issues,
            "data_row_count": len(data_rows), "prematch_quote_count": sum(r["prematch_quote"] for r in rows),
            "suspended_row_count": sum(r["kind"] == "suspended" for r in rows),
            "unrecognized_row_count": sum(r["kind"] == "unrecognized_row" for r in rows),
            "issues": dict(sorted(issues.items())), "pagination_controls": visible_controls,
            "projection_truncated": bool(projection_truncated),
            "same_minute_groups": {k: v for k, v in time_groups.items() if v > 1},
            "source_order": "preserved; chronological order within minute not certified",
            "earliest_visible_quote": _endpoint(rows, earliest=True),
            "last_visible_prematch_quote": _endpoint(rows, cutoff=kickoff.strftime("%Y-%m-%d %H:%M"), exclusive_cutoff=True),
            "last_change_at_or_before_t15": _endpoint(rows, cutoff=cutoff),
            "rows_sha256": raw_digest, "rows": rows}
