from datetime import datetime, timedelta, timezone

import four_team_monitor as monitor


TZ = timezone(timedelta(hours=8), name="Asia/Hong_Kong")


def row(minutes: int, match_id: int = 1):
    kickoff = datetime(2026, 9, 20, 20, 0, tzinfo=TZ)
    return {
        "matchId": match_id,
        "code": "周日001",
        "home": "阿森纳",
        "away": "曼联",
        "matchDate": kickoff.date().isoformat(),
        "matchTime": kickoff.time().isoformat(),
        "_now": kickoff.timestamp() - minutes * 60,
    }


def test_early_then_final_are_distinct_review_stages(monkeypatch):
    monkeypatch.setattr(monitor, "_tracked_ids", lambda: {"arsenal", "man-united"})
    early = row(120)
    early_now = datetime.fromtimestamp(early.pop("_now"), TZ)
    found = monitor.find_due(early_now, [early], {})
    assert found[0]["stage"] == "early"

    final = row(45)
    final_now = datetime.fromtimestamp(final.pop("_now"), TZ)
    found = monitor.find_due(final_now, [final], {"1": {"early": "done"}})
    assert found[0]["stage"] == "final"


def test_sent_stage_is_not_repeated(monkeypatch):
    monkeypatch.setattr(monitor, "_tracked_ids", lambda: {"arsenal"})
    match = row(45)
    now = datetime.fromtimestamp(match.pop("_now"), TZ)
    assert monitor.find_due(now, [match], {"1": {"final": "done"}}) == []


def test_untracked_match_is_ignored(monkeypatch):
    monkeypatch.setattr(monitor, "_tracked_ids", lambda: {"brighton"})
    match = row(45)
    now = datetime.fromtimestamp(match.pop("_now"), TZ)
    assert monitor.find_due(now, [match], {}) == []


def test_outcome_parsing():
    assert monitor._outcome(monitor._score("2:1")) == "主胜"
    assert monitor._outcome(monitor._score("1-1")) == "平"
    assert monitor._outcome(monitor._score("0:2")) == "客胜"


def test_report_combines_same_day_f2_and_f3(tmp_path, monkeypatch):
    context = tmp_path / "context.json"
    state = tmp_path / "state.json"
    report = tmp_path / "report.md"
    predictions = tmp_path / "predictions"
    f2_dir = tmp_path / "f2"
    predictions.mkdir()
    f2_dir.mkdir()
    context.write_text("""{
      "matches": [{
        "matchId": 9, "monitorKey": "9", "stage": "final", "code": "周日001",
        "home": "阿森纳", "away": "曼联", "matchDate": "2026-09-20", "matchTime": "20:00:00",
        "had": {"h": "1.80", "d": "3.60", "a": "4.20"}
      }]
    }""", encoding="utf-8")
    (predictions / "2026-09-20-boldplay.json").write_text("""{
      "tiers": {"base": {"legs": [{
        "matchNumStr": "周日001", "match": "阿森纳-曼联", "play": "had",
        "pick": "主胜", "odds": 1.80, "p": 0.60
      }]}}
    }""", encoding="utf-8")
    (f2_dir / "2026-09-20T100000Z-premier_league.json").write_text("""{
      "capturedAt": "2026-09-20T10:00:00+00:00",
      "matches": [{
        "home_team": "Arsenal", "away_team": "Manchester United",
        "kickoff_utc": "2026-09-20T12:00:00Z",
        "predictions": [{"type": "full_time_score", "value": "2:1"}]
      }]
    }""", encoding="utf-8")
    monkeypatch.setattr(monitor, "CONTEXT", context)
    monkeypatch.setattr(monitor, "STATE", state)
    monkeypatch.setattr(monitor, "REPORT", report)
    monkeypatch.setattr(monitor, "PREDICTIONS", predictions)
    monkeypatch.setattr(monitor, "F2_DIR", f2_dir)
    monkeypatch.setattr(monitor, "_alias_index", lambda: {
        "阿森纳": "arsenal", "arsenal": "arsenal",
        "曼联": "man-united", "manchesterunited": "man-united",
    })

    monitor.report(datetime(2026, 9, 20, 19, 15, tzinfo=TZ))

    text = report.read_text(encoding="utf-8")
    assert "f2：2:1" in text
    assert "f3：HAD 主胜 @ 1.8" in text
    assert "最低赔率：1.67" in text
    assert "可选" in text
    assert "final" in state.read_text(encoding="utf-8")
