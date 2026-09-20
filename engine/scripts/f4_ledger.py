"""Append-only f4 observations and fixed-snapshot forward evaluation.

Probability order and deterministic top-one tie order are H, D, A. This module
has no network or model dependencies and never fills missing observations.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _instant(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("an explicit timezone is required")
    return parsed.astimezone(timezone.utc)


def _stamp(value):
    return _instant(value).isoformat().replace("+00:00", "Z")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _identity(row):
    for key in ("match_id", "league", "home_id", "away_id"):
        if not isinstance(row.get(key), str) or not row[key].strip() or row[key] != row[key].strip():
            raise ValueError(f"nonempty canonical {key} is required")
    if row["home_id"] == row["away_id"]:
        raise ValueError("home and away canonical teams must differ")


def _probability(value, name):
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{name} must contain H/D/A probabilities or be null")
    if any(isinstance(p, bool) or not isinstance(p, (int, float))
           or not math.isfinite(p) or not 0 <= p <= 1 for p in value):
        raise ValueError(f"{name} contains an invalid probability")
    if not math.isclose(sum(value), 1.0, abs_tol=1e-9, rel_tol=0):
        raise ValueError(f"{name} must sum to one")
    return list(value)


def _score(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2 or any(type(v) is not int or v < 0 for v in value):
        raise ValueError("a pair of nonnegative integer scores is required")
    return list(value)


def _direction(score):
    return 0 if score[0] > score[1] else (1 if score[0] == score[1] else 2)


def _prediction(record):
    row = json.loads(_json(record))  # copy, rejecting non-JSON values and NaNs everywhere
    _identity(row)
    for key in ("experiment_id", "horizon"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError(f"nonempty {key} is required")
    for key in ("kickoff_at", "captured_at", "decision_at", "generated_at"):
        row[key] = _stamp(row[key])
    captured, decision, generated, kickoff = (_instant(row[k]) for k in
                                              ("captured_at", "decision_at", "generated_at", "kickoff_at"))
    if not captured <= decision <= generated < kickoff:
        raise ValueError("require captured_at <= decision_at <= generated_at < kickoff_at")
    targets = {"T-180m": 180, "T-60m": 60}
    if row["horizon"] not in targets:
        raise ValueError("only the frozen T-180m and T-60m horizons are supported")
    tolerance = row.get("horizon_tolerance_minutes", 20)
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or not 0 <= tolerance <= 20:
        raise ValueError("horizon tolerance must be finite and at most 20 minutes")
    actual = (kickoff - decision).total_seconds() / 60
    if abs(actual - targets[row["horizon"]]) > tolerance:
        raise ValueError("decision is outside its declared horizon window")
    if "actual_minutes" in row:
        declared = row["actual_minutes"]
        if isinstance(declared, bool) or not isinstance(declared, (int, float)) or not math.isfinite(declared) or not math.isclose(declared, actual, abs_tol=1e-6):
            raise ValueError("actual_minutes does not match the decision and kickoff")
    if type(row.get("selected")) is not bool:
        raise ValueError("selected must be a boolean")
    for key in ("p_baseline", "p_champion", "p_candidate"):
        row[key] = _probability(row.get(key), key)
    if row["p_candidate"] is not None and (not isinstance(row.get("candidate_version"), str) or not row["candidate_version"].strip()):
        raise ValueError("candidate probabilities require an explicit candidate_version")
    if row.get("trained_through") is not None:
        row["trained_through"] = _stamp(row["trained_through"])
        if _instant(row["trained_through"]) >= decision:
            raise ValueError("declared training cutoff must precede the decision")
    if row.get("p_f2") is not None:
        raise ValueError("f2 point forecasts must not be converted to probabilities")
    point = row.get("f2")
    if point is not None:
        if not isinstance(point, dict):
            raise ValueError("f2 must be a point-forecast observation")
        if any(point.get(k) is not None for k in ("probabilities", "probability", "p", "p_f2")):
            raise ValueError("f2 is categorical only")
        point["captured_at"] = _stamp(point["captured_at"])
        if _instant(point["captured_at"]) > captured:
            raise ValueError("f2 capture must be included in the input capture cutoff")
        if point.get("published_at") is not None:
            point["published_at"] = _stamp(point["published_at"])
            if _instant(point["published_at"]) > decision:
                raise ValueError("f2 publication is later than the decision")
        for key in ("home_id", "away_id"):
            if point.get(key) != row[key]:
                raise ValueError("f2 canonical sides do not match")
        point["event_kickoff_at"] = _stamp(point["event_kickoff_at"])
        if abs((_instant(point["event_kickoff_at"]) - kickoff).total_seconds()) > 300:
            raise ValueError("f2 event kickoff does not match")
        point["score"] = _score(point["score"])
        if point.get("direction") != "HDA"[_direction(point["score"])]:
            raise ValueError("f2 direction does not match its point score")
    return row


def _snapshot_key(row):
    return row["match_id"], row["kickoff_at"], row["horizon"], row["experiment_id"]


def _write_once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(body)
        return True
    except FileExistsError:
        if _json(json.loads(path.read_text(encoding="utf-8"))) != _json(value):
            raise ValueError(f"immutable ledger identity conflicts with existing record: {path.name}")
        return False


def save_prediction(root, record):
    """Save one immutable decision; a conflicting retry never overwrites it."""
    row = _prediction(record)
    key = (*_snapshot_key(row), row["decision_at"])
    path = Path(root) / "data/f4/predictions" / row["kickoff_at"][:10] / (_hash(key) + ".json")
    return path, _write_once(path, row)


def load_predictions(root):
    rows = [_prediction(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted((Path(root) / "data/f4/predictions").glob("*/*.json"))]
    return sorted(rows, key=lambda row: (_instant(row["decision_at"]), _instant(row["generated_at"]), _hash(row)))


def _outcome(fixture, observed_at):
    row = json.loads(_json(fixture))
    _identity(row)
    row["kickoff_at"] = _stamp(row["kickoff_at"])
    row["observed_at"] = _stamp(observed_at)
    raw = str(row.get("status") or "unknown").lower().replace(" ", "_")
    extra = {str(row.get(k, "")).lower() for k in ("status", "raw_status", "source_status", "status_detail")}
    extra_time = any(v in {"aet", "pen", "after_extra_time", "after_penalties", "final_after_extra_time", "final_after_penalties"} for v in extra)
    aliases = {"ft": "completed", "finished": "completed", "full_time": "completed", "canceled": "cancelled",
               "in_progress": "live", "inprogress": "live", "not_started": "scheduled", "ns": "scheduled"}
    status = aliases.get(raw, raw)
    if status not in {"completed", "postponed", "cancelled", "live", "scheduled", "unknown"}:
        status = "unknown"
    score = None
    if extra_time:
        explicit = row.get("score90", row.get("score_90"))
        if explicit is None and ("home_score90" in row or "away_score90" in row):
            explicit = [row.get("home_score90"), row.get("away_score90")]
        if explicit is None and ("home_score_90" in row or "away_score_90" in row):
            explicit = [row.get("home_score_90"), row.get("away_score_90")]
        if explicit is None:
            status = "unknown"
            row["settlement_reason"] = "extra time or penalties without an explicit 90-minute score"
        else:
            score, status = _score(explicit), "completed"
    elif status == "completed":
        score = _score([row.get("home_score"), row.get("away_score")])
    if status == "completed" and _instant(row["observed_at"]) <= _instant(row["kickoff_at"]):
        raise ValueError("a completed result must be observed after kickoff")
    row.update(status=status, home_score=score[0] if score else None, away_score=score[1] if score else None)
    return row


def _outcome_semantic(row):
    return {key: row[key] for key in ("match_id", "league", "home_id", "away_id", "kickoff_at", "status", "home_score", "away_score")}


def _load_outcomes(root):
    rows = []
    for path in sorted((Path(root) / "data/f4/outcomes").glob("*/*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        rows.append(_outcome(row, row["observed_at"]))
    return sorted(rows, key=lambda row: (_instant(row["observed_at"]), _hash(_outcome_semantic(row))))


def load_latest_outcomes(root):
    """Return the latest observed revision for each provider-stable match id."""
    return {row["match_id"]: row for row in _load_outcomes(root)}


def save_outcome(root, fixture, observed_at):
    """Append a new semantic revision; repeating the latest state is a no-op."""
    row = _outcome(fixture, observed_at)
    existing = [old for old in _load_outcomes(root) if old["match_id"] == row["match_id"]]
    predictions = [old for old in load_predictions(root) if old["match_id"] == row["match_id"]]
    for old in [*existing, *predictions]:
        if any(old[key] != row[key] for key in ("league", "home_id", "away_id")):
            raise ValueError("canonical event sides or league conflict with the existing ledger")
    if existing and _outcome_semantic(existing[-1]) == _outcome_semantic(row):
        return False
    if any(old["observed_at"] == row["observed_at"] and _outcome_semantic(old) != _outcome_semantic(row) for old in existing):
        raise ValueError("conflicting outcomes at the same observation instant")
    observed = _instant(row["observed_at"]).strftime("%Y%m%dT%H%M%S%fZ")
    path = Path(root) / "data/f4/outcomes" / _hash(row["match_id"]) / (observed + "-" + _hash(_outcome_semantic(row)) + ".json")
    # Metadata variation on the same observation does not create a score revision.
    if path.exists():
        return False
    return _write_once(path, row)


def _metrics(rows):
    count = len(rows)
    if not count:
        return {"n": 0, "correct": 0, "accuracy": None, "rps": None, "brier": None, "log_loss": None}
    correct, rps, brier, loss = 0, 0.0, 0.0, 0.0
    for probability, actual in rows:
        correct += max(range(3), key=lambda i: probability[i]) == actual
        truth = [int(i == actual) for i in range(3)]
        rps += sum((sum(probability[:i + 1]) - sum(truth[:i + 1])) ** 2 for i in range(2)) / 2
        brier += sum((probability[i] - truth[i]) ** 2 for i in range(3))
        loss -= math.log(max(probability[actual], 1e-15))
    return {"n": count, "correct": correct, "accuracy": correct / count, "rps": rps / count,
            "brier": brier / count, "log_loss": loss / count}


def _categorical(rows):
    count = len(rows)
    correct = sum(predicted == actual for predicted, actual in rows)
    return {"n": count, "correct": correct, "accuracy": correct / count if count else None}


_COUNT_KEYS = ("raw_snapshots", "effective_valid", "missing_baseline", "missing_baseline_groups",
               "redundant_snapshots", "pending", "settled", "paired", "missing_candidate",
               "excluded_rescheduled", "excluded_cancelled", "excluded_postponed", "excluded_identity", "excluded_future")


def _bucket():
    return {"counts": dict.fromkeys(_COUNT_KEYS, 0), "baseline": [], "paired_baseline": [],
            "candidate": [], "f2": [], "f2_paired_baseline": [], "pending_statuses": defaultdict(int)}


def _group_key(row):
    return row["league"], row["horizon"], row.get("candidate_version") or "unavailable", row["experiment_id"]


def _finish(bucket):
    return {"counts": bucket["counts"], "baseline": _metrics(bucket["baseline"]),
            "paired_baseline": _metrics(bucket["paired_baseline"]), "candidate": _metrics(bucket["candidate"]),
            "f2": _categorical(bucket["f2"]), "f2_paired_baseline": _categorical(bucket["f2_paired_baseline"]),
            "pending_statuses": dict(bucket["pending_statuses"])}


def _coverage(root, rows, latest, checked):
    """Coverage of locally discovered fixtures, never a claim about the universe."""
    config_path = Path(root) / "data/f4/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    experiment = config.get("experiment_id")
    predictions = {(_snapshot_key(row)) for row in rows if row["p_baseline"] is not None
                   and _instant(row["generated_at"]) <= checked}
    attempts = {_snapshot_key(row) for row in rows if row["p_baseline"] is None
                and _instant(row["generated_at"]) <= checked}
    fixtures = {}
    unresolved = 0
    known_ids = set()
    for path in sorted((Path(root) / "data/f4/fixtures").glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        known_ids.add(row.get("match_id") or str(path))
        try:
            _identity(row)
            row["kickoff_at"] = _stamp(row["kickoff_at"])
            row["last_seen_at"] = _stamp(row.get("last_seen_at") or row.get("first_seen_at"))
        except (ValueError, TypeError, KeyError):
            unresolved += 1
            continue
        old = fixtures.get(row["match_id"])
        if old is None or _instant(row["last_seen_at"]) > _instant(old["last_seen_at"]):
            fixtures[row["match_id"]] = row
    result = {"scope": "locally discovered target fixtures only; source completeness is not established",
              "known_tracked_fixtures": len(known_ids), "unresolved_fixtures": unresolved, "closed_windows": 0, "covered_windows": 0,
              "missing_windows": 0, "missing_baseline_windows": 0, "late_discovered_windows": 0,
              "unknown_discovery_windows": 0, "excluded_cancelled_or_postponed_fixtures": 0}
    for original in fixtures.values():
        row = original
        outcome = latest.get(row["match_id"])
        if outcome and _instant(outcome["observed_at"]) >= _instant(row["last_seen_at"]):
            row = {**row, **outcome}
        if row.get("status") in {"cancelled", "postponed"}:
            result["excluded_cancelled_or_postponed_fixtures"] += 1
            continue
        for target in (180, 60):
            end = _instant(row["kickoff_at"]) - timedelta(minutes=target - 20)
            if checked <= end:
                continue
            if not original.get("first_seen_at"):
                result["unknown_discovery_windows"] += 1
                continue
            if _instant(original["first_seen_at"]) > end:
                result["late_discovered_windows"] += 1
                continue
            result["closed_windows"] += 1
            prefix = (row["match_id"], _stamp(row["kickoff_at"]), f"T-{target}m")
            has = lambda collection: any(key[:3] == prefix and (experiment is None or key[3] == experiment) for key in collection)
            if has(predictions):
                result["covered_windows"] += 1
            else:
                result["missing_windows"] += 1
                result["missing_baseline_windows"] += int(has(attempts))
    return result


def evaluate_ledger(root, now):
    """Evaluate the first usable baseline snapshot per match/kickoff/stage/run.

    Candidate absence is locked at that snapshot. A later candidate is never
    cherry-picked, and candidate comparisons use exactly the paired fixtures.
    """
    checked = _instant(now)
    rows = load_predictions(root)
    latest = {row["match_id"]: row for row in _load_outcomes(root) if _instant(row["observed_at"]) <= checked}
    total, groups, snapshots = _bucket(), defaultdict(_bucket), defaultdict(list)
    for row in rows:
        for bucket in (total, groups[_group_key(row)]):
            bucket["counts"]["raw_snapshots"] += 1
            if row["p_baseline"] is None:
                bucket["counts"]["missing_baseline"] += 1
            if _instant(row["generated_at"]) > checked:
                bucket["counts"]["excluded_future"] += 1
        if _instant(row["generated_at"]) <= checked:
            snapshots[_snapshot_key(row)].append(row)
    effective = []
    for choices in snapshots.values():
        eligible = [row for row in choices if row["p_baseline"] is not None]
        if not eligible:
            for bucket in (total, groups[_group_key(choices[0])]):
                bucket["counts"]["missing_baseline_groups"] += 1
            continue
        row = eligible[0]
        effective.append(row)
        for bucket in (total, groups[_group_key(row)]):
            bucket["counts"]["effective_valid"] += 1
            bucket["counts"]["redundant_snapshots"] += len(eligible) - 1
    for row in effective:
        outcome = latest.get(row["match_id"])
        buckets = (total, groups[_group_key(row)])
        exclusion = None
        if outcome:
            if any(row[key] != outcome[key] for key in ("league", "home_id", "away_id")):
                exclusion = "excluded_identity"
            elif row["kickoff_at"] != outcome["kickoff_at"]:
                exclusion = "excluded_rescheduled"
            elif outcome["status"] in {"cancelled", "postponed"}:
                exclusion = "excluded_" + outcome["status"]
        if exclusion:
            for bucket in buckets:
                bucket["counts"][exclusion] += 1
            continue
        if not outcome or outcome["status"] != "completed":
            for bucket in buckets:
                bucket["counts"]["pending"] += 1
                bucket["pending_statuses"][outcome["status"] if outcome else "unobserved"] += 1
            continue
        actual = _direction([outcome["home_score"], outcome["away_score"]])
        for bucket in buckets:
            bucket["counts"]["settled"] += 1
            bucket["baseline"].append((row["p_baseline"], actual))
            if row["p_candidate"] is not None:
                bucket["counts"]["paired"] += 1
                bucket["paired_baseline"].append((row["p_baseline"], actual))
                bucket["candidate"].append((row["p_candidate"], actual))
            else:
                bucket["counts"]["missing_candidate"] += 1
            if row.get("f2"):
                bucket["f2"].append(("HDA".index(row["f2"]["direction"]), actual))
                bucket["f2_paired_baseline"].append((max(range(3), key=lambda i: row["p_baseline"][i]), actual))
    summary = {"schema_version": 1, "evaluated_at": _stamp(checked), "mode": "shadow", "candidate_weight": 0,
               "automatic_promotion": False, "certified_edge": False, "training_independently_verified": False,
               "snapshot_policy": "earliest eligible baseline; candidate availability locked to that snapshot",
               "score_policy": {"probability_order": ["H", "D", "A"], "tie_order": ["H", "D", "A"],
                                "rps_divisor": 2, "brier_scaled": False, "log_loss_base": "natural", "log_loss_floor": 1e-15},
               **_finish(total), "groups": []}
    for key, bucket in sorted(groups.items()):
        summary["groups"].append({**dict(zip(("league", "horizon", "candidate_version", "experiment_id"), key)), **_finish(bucket)})
    summary["training_evidence"] = sorted({str(row.get("candidate_training_evidence") or "unavailable") for row in effective})
    summary["counts"]["distinct_match_count"] = len({row["match_id"] for row in effective})
    summary["coverage"] = _coverage(root, rows, latest, checked)
    for key in ("known_tracked_fixtures", "closed_windows", "covered_windows", "missing_windows", "late_discovered_windows"):
        summary["counts"][key] = summary["coverage"][key]
    return summary


def render_report(summary):
    """Return a Chinese Markdown report without changing the ledger."""
    counts = summary["counts"]
    def percent(value):
        return "—（无已结算样本）" if value is None else f"{value:.1%}"
    def number(value):
        return "—" if value is None else f"{value:.4f}"
    lines = ["# f4 前向对照周报", "", f"评估时间：{summary['evaluated_at']}（UTC）。", "",
             "候选为冻结的旧 Dixon–Coles（DC）参数，仅作影子对照；候选权重为 **0**，不自动晋级、不自动下注。",
             "训练截止日期来自历史文件声明，训练资料尚未独立认证；已有结果不构成已认证优势，也不能证明校准有效。", "",
             "## 样本与覆盖", "",
             f"- 原始快照 {counts['raw_snapshots']} 条；按比赛、开球时间、窗口和实验固定首条有效市场基线后，有效记录 {counts['effective_valid']} 条。",
             f"- 缺少市场基线的尝试 {counts['missing_baseline']} 条，其中 {counts['missing_baseline_groups']} 组始终缺失；重复有效快照 {counts['redundant_snapshots']} 条不重复计分。",
             f"- 有效记录涉及 {counts.get('distinct_match_count', 0)} 场不同比赛；已结算 {counts['settled']} 条、候选与基线同场配对 {counts['paired']} 条、待赛果 {counts['pending']} 条。总体条数按赛前窗口统计，同一比赛两个窗口不算两场独立比赛。",
             f"- 排除：改期后开球时间不匹配 {counts['excluded_rescheduled']} 条、取消 {counts['excluded_cancelled']} 条、延期 {counts['excluded_postponed']} 条、球队身份不符 {counts['excluded_identity']} 条。",
             f"- 本地已发现目标赛程 {counts.get('known_tracked_fixtures', 0)} 场；在窗口关闭前已知的窗口 {counts.get('closed_windows', 0)} 个，其中已留有效基线 {counts.get('covered_windows', 0)} 个、缺失 {counts.get('missing_windows', 0)} 个；窗口关闭后才发现 {counts.get('late_discovered_windows', 0)} 个，另列而不计入及时覆盖分母。",
             "- 无赛果、延期、取消均不计为预测失败；空样本命中率显示缺失，不显示 0%。账本样本量不代表赛程或赔率源覆盖完整。", "",
             "## 分组指标", "",
             "市场基线的全样本成绩仅供覆盖检查；比较候选时使用同场配对基线。不同联赛、窗口、候选版本和实验分别报告。", ""]
    if not summary["groups"]:
        lines.append("尚无预测记录，暂无可评估成绩。")
    for group in summary["groups"]:
        label = " / ".join(str(group[key]).replace("|", "\\|").replace("\n", " ") for key in
                           ("league", "horizon", "candidate_version", "experiment_id"))
        lines.extend([f"### {label}", "", "| 模型及样本 | n | 胜平负命中率 | RPS ↓ | Brier ↓ | Log loss ↓ |",
                      "|---|---:|---:|---:|---:|---:|"])
        for title, key in (("市场基线（全部已结算）", "baseline"), ("市场基线（与 DC 同场）", "paired_baseline"), ("固定 DC 候选（同场）", "candidate")):
            metric = group[key]
            lines.append(f"| {title} | {metric['n']} | {percent(metric['accuracy'])} | {number(metric['rps'])} | {number(metric['brier'])} | {number(metric['log_loss'])} |")
        point = group["f2"]
        lines.extend(["", f"f2 点预测：{point['n']} 场，胜平负方向命中率 {percent(point['accuracy'])}；对应同场市场方向命中率 {percent(group['f2_paired_baseline']['accuracy'])}。f2 不转换概率，不计算 RPS、Brier 或 Log loss。", ""])
    lines.extend(["## 评分约定", "", "概率与并列最高概率的优先顺序均为主胜 H、平局 D、客胜 A。RPS 为前两项累计概率误差平方和除以 2；Brier 为三类误差平方和，不缩放；Log loss 使用自然对数，零概率按 10⁻¹⁵ 下限计算。",
                  "每组采用最早一条含有效市场基线的赛前快照。缺基线可重试；该快照若缺候选，后续候选不会补入。只按明确的 90 分钟比分结算，改期后的新开球时间必须另行采集赛前快照。"])
    return "\n".join(lines).rstrip() + "\n"
