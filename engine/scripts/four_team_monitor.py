#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloud-friendly monitor for the four tracked Premier League teams.

The check phase is cheap and only opens an analysis window when a tracked
Sporttery match is close to kick-off.  The report phase reads the deterministic
f3 ticket output plus the saved f2 forecast and creates a conservative Markdown
notification.  It never places a wager.

Usage:
  python four_team_monitor.py check [--now 2026-09-14T20:00:00+08:00]
  python four_team_monitor.py report
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import ROOT, load_aliases, log

TZ = timezone(timedelta(hours=8), name="Asia/Hong_Kong")
CONFIG = ROOT / "data" / "05-trends" / "tracked-teams-2026-27.json"
SPORTTERY = ROOT / "engine" / "cache" / "sporttery_matches.json"
STATE = ROOT / "engine" / "cache" / "four_team_monitor_state.json"
CONTEXT = ROOT / ".github" / "run-logs" / "four-team-context.json"
REPORT = ROOT / "data" / "04-summaries" / "four-team-latest.md"
F2_DIR = ROOT / "data" / "05-trends" / "f2"
PREDICTIONS = ROOT / "data" / "03-predictions"

EARLY_MINUTES = 180
FINAL_MINUTES = 75


def _norm(value: str | None) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", str(value or "").casefold())


def _alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for canonical, values in load_aliases().items():
        for value in [canonical, *(v for v in values.values() if isinstance(v, str))]:
            index[_norm(value)] = canonical
    return index


def _read(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _display_path(path: Path) -> Path:
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


def _write_output(**values: object) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            fh.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")


def _kickoff(row: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(
            f"{row['matchDate']}T{row['matchTime']}"
        ).replace(tzinfo=TZ)
    except (KeyError, TypeError, ValueError):
        return None


def _tracked_ids() -> set[str]:
    config = _read(CONFIG, {})
    return {str(t.get("canonicalId")) for t in config.get("teams", []) if t.get("canonicalId")}


def _candidate_stage(minutes: float) -> str | None:
    if 0 <= minutes <= FINAL_MINUTES:
        return "final"
    if FINAL_MINUTES < minutes <= EARLY_MINUTES:
        return "early"
    return None


def find_due(now: datetime, matches: list[dict], state: dict) -> list[dict]:
    aliases = _alias_index()
    tracked = _tracked_ids()
    due = []
    for row in matches:
        home_id = aliases.get(_norm(row.get("home")))
        away_id = aliases.get(_norm(row.get("away")))
        if home_id not in tracked and away_id not in tracked:
            continue
        kickoff = _kickoff(row)
        if kickoff is None:
            continue
        minutes = (kickoff - now.astimezone(TZ)).total_seconds() / 60
        stage = _candidate_stage(minutes)
        key = str(row.get("matchId") or row.get("code") or f"{row.get('matchDate')}-{home_id}-{away_id}")
        if not stage or state.get(key, {}).get(stage):
            continue
        due.append({**row, "monitorKey": key, "stage": stage, "minutesToKickoff": round(minutes, 1)})
    return sorted(due, key=lambda row: row["minutesToKickoff"])


def check(now: datetime) -> list[dict]:
    cache = _read(SPORTTERY, {})
    state = _read(STATE, {})
    due = find_due(now, cache.get("matches") or [], state)
    CONTEXT.parent.mkdir(parents=True, exist_ok=True)
    CONTEXT.write_text(json.dumps({"checkedAt": now.isoformat(), "matches": due}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_output(notify=bool(due), context=CONTEXT.as_posix())
    log("four-team", f"{len(due)} tracked match(es) due for review")
    return due


def _walk_legs(value):
    if isinstance(value, dict):
        if value.get("matchNumStr") and value.get("pick"):
            yield value
        for child in value.values():
            yield from _walk_legs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_legs(child)


def _latest_card() -> dict:
    cards = sorted(PREDICTIONS.glob("*-boldplay.json"), reverse=True)
    return _read(cards[0], {}) if cards else {}


def _score(value: str | None) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\s*[:\-]\s*(\d+)", str(value or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


def _outcome(score: tuple[int, int] | None) -> str | None:
    if score is None:
        return None
    return "主胜" if score[0] > score[1] else ("平" if score[0] == score[1] else "客胜")


def _f2_value(match: dict) -> str | None:
    for item in match.get("predictions") or []:
        kind = str(item.get("type") or "").casefold()
        if "full" in kind and ("time" in kind or "score" in kind):
            if _score(item.get("value")):
                return str(item.get("value"))
    for key in ("full_time_score", "fulltime_score", "predicted_score"):
        if _score(match.get(key)):
            return str(match[key])
    return None


def _f2_date(row: dict, fallback: str) -> str:
    raw = row.get("kickoff_utc") or row.get("kickoff") or row.get("kickoff_formatted")
    match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", str(raw or ""))
    if not match:
        return fallback
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def _latest_f2_predictions() -> dict[tuple[str, str, str], tuple[str, str]]:
    aliases = _alias_index()
    index: dict[tuple[str, str, str], tuple[str, str]] = {}
    for path in sorted(F2_DIR.glob("*.json"), reverse=True)[:8] if F2_DIR.exists() else []:
        data = _read(path, {})
        captured_at = str(data.get("capturedAt") or "")
        fallback = captured_at[:10] or path.name[:10]
        for row in data.get("matches") or []:
            home = aliases.get(_norm(row.get("home_team")), _norm(row.get("home_team")))
            away = aliases.get(_norm(row.get("away_team")), _norm(row.get("away_team")))
            value = _f2_value(row)
            if value:
                index.setdefault((_f2_date(row, fallback), home, away), (value, captured_at))
    return index


def _format_odds(row: dict) -> str:
    had = row.get("had") or {}
    if not had:
        return "未取得胜平负赔率"
    return f"主 {had.get('h', '—')} / 平 {had.get('d', '—')} / 客 {had.get('a', '—')}"


def _best_leg(legs: list[dict]) -> dict | None:
    if not legs:
        return None
    def value(leg: dict) -> float:
        if leg.get("ev") is not None:
            return float(leg["ev"])
        p, odds = leg.get("p"), leg.get("odds")
        return float(p) * float(odds) - 1 if p and odds else -math.inf
    return max(legs, key=value)


def report(now: datetime) -> Path:
    context = _read(CONTEXT, {})
    matches = context.get("matches") or []
    card = _latest_card()
    all_legs = list(_walk_legs(card.get("tiers") or {}))
    f2 = _latest_f2_predictions()
    aliases = _alias_index()
    state = _read(STATE, {})
    lines = [
        f"# 四队自动复核 · {now.astimezone(TZ).strftime('%Y-%m-%d %H:%M')}",
        "",
        "> 云端自动生成；只提供可核查的赛前判断，不执行下注。",
        "",
    ]
    for row in matches:
        code = str(row.get("code") or "")
        relevant = [leg for leg in all_legs if str(leg.get("matchNumStr") or "") == code]
        leg = _best_leg(relevant)
        home_id = aliases.get(_norm(row.get("home")), _norm(row.get("home")))
        away_id = aliases.get(_norm(row.get("away")), _norm(row.get("away")))
        f2_item = f2.get((str(row.get("matchDate") or ""), home_id, away_id))
        f2_score, f2_at = f2_item if f2_item else (None, None)
        conflict = bool(
            leg and str(leg.get("play", "")).casefold() == "had" and f2_score
            and _outcome(_score(f2_score)) != str(leg.get("pick"))
        )
        if leg and not conflict:
            probability = leg.get("p") if leg.get("p") is not None else leg.get("q")
            threshold = round(1 / float(probability), 2) if probability else None
            verdict = "可选（B级证据，仍以票面赔率不低于门槛为条件）"
        elif conflict:
            probability = None
            threshold = None
            verdict = "跳过：f2 与 f3 方向冲突"
        else:
            probability = None
            threshold = None
            verdict = "跳过：f3 本轮没有选中该场"
        kickoff = _kickoff(row)
        lines.extend([
            f"## {row.get('home')} vs {row.get('away')}",
            "",
            f"- 场次：{code}；开赛：{kickoff.strftime('%Y-%m-%d %H:%M') if kickoff else '未知'}（香港时间）",
            f"- 当前胜平负：{_format_odds(row)}",
            f"- f2：{f2_score or '本次未返回预测'}" + (f"（快照 {f2_at}）" if f2_score and f2_at else ""),
            f"- f3：{leg.get('play', '').upper()} {leg.get('pick')} @ {leg.get('odds')}" if leg else "- f3：无合格选项",
            f"- 模型概率：{float(probability):.1%}；最低赔率：{threshold}" if probability else "- 模型概率／最低赔率：无合格值",
            f"- 结论：**{verdict}**",
            "",
        ])
        key, stage = row["monitorKey"], row["stage"]
        state.setdefault(key, {})[stage] = now.isoformat()
        state[key]["match"] = f"{row.get('home')} vs {row.get('away')}"
        state[key]["kickoff"] = kickoff.isoformat() if kickoff else None
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    day = str(matches[0].get("matchDate")) if matches else now.astimezone(TZ).date().isoformat()
    _write_output(report=REPORT.as_posix(), issue_title=f"[四队监控] {day} 赛前复核")
    log("four-team", f"report written to {_display_path(REPORT)}")
    return REPORT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "report"))
    parser.add_argument("--now", help="ISO-8601 time override for testing")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    check(now) if args.command == "check" else report(now)


if __name__ == "__main__":
    main()
