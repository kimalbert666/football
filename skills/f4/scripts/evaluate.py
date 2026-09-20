"""Audit frozen 90-minute H/D/A probabilities; never train or promote a model.

JSONL fields (all required): match_id, league, horizon, model_version (strings),
kickoff_at, decision_at, captured_at, trained_through (timezone-aware ISO times),
outcome (H/D/A or null), p_baseline, p_candidate (three probabilities), selected
(boolean). Files must contain one league, horizon, and candidate model version.
Horizons use T-<positive integer>m; actual lead time must match the declared
horizon within an explicitly prespecified tolerance (default zero minutes).
Timestamps are declarations, not evidence of provenance or held-out training.
"""
import argparse
import json
import math
import random
import re
from datetime import datetime, timezone
from pathlib import Path

ORDER = ("H", "D", "A")
TIME_FIELDS = ("kickoff_at", "decision_at", "captured_at", "trained_through")
TEXT_FIELDS = ("match_id", "league", "horizon", "model_version")
REQUIRED = set(TIME_FIELDS + TEXT_FIELDS) | {
    "outcome", "p_baseline", "p_candidate", "selected"
}


def validate(rows, horizon_tolerance_minutes=0):
    """Validate every row, including pending outcomes; return copied records."""
    if (isinstance(horizon_tolerance_minutes, bool)
            or not isinstance(horizon_tolerance_minutes, (int, float))
            or horizon_tolerance_minutes < 0 or not math.isfinite(horizon_tolerance_minutes)):
        raise ValueError("horizon_tolerance_minutes must be nonnegative and finite")
    if not rows:
        raise ValueError("Input contains no records")
    validated, seen, scope = [], set(), None
    for line, row in enumerate(rows, 1):
        prefix = f"Record {line}: "
        if not isinstance(row, dict) or not REQUIRED <= row.keys():
            raise ValueError(prefix + "missing required fields or not a JSON object")
        if any(not isinstance(row[k], str) or not row[k].strip() for k in TEXT_FIELDS):
            raise ValueError(prefix + "identifiers, scope, and version must be nonempty strings")
        if row["match_id"] in seen:
            raise ValueError(prefix + "duplicate match_id (including repeated stages)")
        seen.add(row["match_id"])
        row_scope = tuple(row[k] for k in ("league", "horizon", "model_version"))
        if scope is not None and row_scope != scope:
            raise ValueError(prefix + "mixed league, horizon, or model_version")
        scope = row_scope
        times = {}
        for key in TIME_FIELDS:
            try:
                value = row[key]
                if not isinstance(value, str):
                    raise ValueError("not a string")
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.utcoffset() is None:
                    raise ValueError("missing timezone")
                times[key] = parsed
            except (ValueError, TypeError) as exc:
                raise ValueError(prefix + key + " must be a timezone-aware ISO timestamp") from exc
        if not (times["captured_at"] <= times["decision_at"] < times["kickoff_at"]):
            raise ValueError(prefix + "requires captured_at <= decision_at < kickoff_at")
        if not times["trained_through"] < times["decision_at"]:
            raise ValueError(prefix + "requires trained_through < decision_at")
        horizon = re.fullmatch(r"T-([1-9][0-9]*)m", row["horizon"])
        if horizon is None:
            raise ValueError(prefix + "horizon must match T-<positive integer>m")
        actual_minutes = (times["kickoff_at"] - times["decision_at"]).total_seconds() / 60
        if abs(actual_minutes - int(horizon.group(1))) > horizon_tolerance_minutes:
            raise ValueError(prefix + "kickoff-decision lead time does not match horizon within tolerance")
        if row["outcome"] is not None and row["outcome"] not in ORDER:
            raise ValueError(prefix + "outcome must be H, D, A, or null")
        if not isinstance(row["selected"], bool):
            raise ValueError(prefix + "selected must be boolean")
        for key in ("p_baseline", "p_candidate"):
            probs = row[key]
            if (not isinstance(probs, list) or len(probs) != 3
                    or any(isinstance(p, bool) or not isinstance(p, (int, float))
                           or not 0 <= p <= 1 or not math.isfinite(p) for p in probs)
                    or not math.isclose(sum(probs), 1.0, rel_tol=0, abs_tol=1e-6)):
                raise ValueError(prefix + key + " must be three finite probabilities summing to 1")
        iso = times["kickoff_at"].astimezone(timezone.utc).isocalendar()
        validated.append(dict(row, _week=f"{iso.year}-W{iso.week:02d}"))
    return validated


def read_rows(path):
    rows = []
    with Path(path).open(encoding="utf-8-sig") as source:
        for line_no, line in enumerate(source, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Line {line_no}: invalid JSON") from exc
    return rows


def scores(probabilities, outcome):
    target = ORDER.index(outcome)
    observed = [int(i == target) for i in range(3)]
    rps = sum((sum(probabilities[:k]) - sum(observed[:k])) ** 2
              for k in (1, 2)) / 2
    return {
        "rps": rps,
        "brier": sum((p - y) ** 2 for p, y in zip(probabilities, observed)),
        "log_loss": -math.log(max(probabilities[target], 1e-15)),
        "accuracy": float(max(range(3), key=lambda i: probabilities[i]) == target),
    }


def metrics(rows, key):
    if not rows:
        return None
    individual = [scores(row[key], row["outcome"]) for row in rows]
    return {"n": len(rows), **{
        metric: sum(result[metric] for result in individual) / len(rows)
        for metric in individual[0]
    }}


def percentile(values, q):
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def paired_bootstrap(rows, iterations, seed):
    groups = {}
    for row in rows:
        base = scores(row["p_baseline"], row["outcome"])
        candidate = scores(row["p_candidate"], row["outcome"])
        groups.setdefault(row["_week"], []).append(
            tuple(candidate[key] - base[key] for key in ("rps", "accuracy")))
    blocks = [groups[week] for week in sorted(groups)]
    differences = [item for block in blocks for item in block]
    result = {
        "method": "paired_UTC_ISO_week_cluster_percentile",
        "direction": "candidate_minus_baseline",
        "n_weeks": len(blocks), "replicates": iterations, "seed": seed,
        "rps": {"difference": None, "ci95": None},
        "accuracy": {"difference": None, "ci95": None},
    }
    for index, metric in enumerate(("rps", "accuracy")):
        if differences:
            result[metric]["difference"] = sum(d[index] for d in differences) / len(differences)
    if len(blocks) < 2:
        result["ci_unavailable_reason"] = "Fewer than two UTC kickoff weeks with settled matches"
        return result
    rng, samples = random.Random(seed), [[], []]
    block_sums = [(len(block), *(sum(d[i] for d in block) for i in (0, 1)))
                  for block in blocks]
    for _ in range(iterations):
        sampled = [rng.choice(block_sums) for _ in blocks]
        n = sum(block[0] for block in sampled)
        for i in (0, 1):
            samples[i].append(sum(block[i + 1] for block in sampled) / n)
    for i, metric in enumerate(("rps", "accuracy")):
        result[metric]["ci95"] = [percentile(samples[i], q) for q in (0.025, 0.975)]
    return result


def evaluate(rows, bootstrap=1000, seed=42, horizon_tolerance_minutes=0):
    if isinstance(bootstrap, bool) or not isinstance(bootstrap, int) or bootstrap < 1:
        raise ValueError("bootstrap must be a positive integer")
    rows = validate(rows, horizon_tolerance_minutes)
    settled = [r for r in rows if r["outcome"] is not None]
    selected = [r for r in rows if r["selected"]]
    selected_settled = [r for r in selected if r["outcome"] is not None]
    return {
        "schema_version": 1, "auditOnly": True, "automaticPromotion": False,
        "status": "review_required" if settled else "insufficient_data",
        "scope": {**{key: rows[0][key] for key in ("league", "horizon", "model_version")},
                  "horizon_tolerance_minutes": horizon_tolerance_minutes},
        "probability_order": list(ORDER),
        "coverage": {
            "n_total": len(rows), "n_settled": len(settled),
            "n_pending": len(rows) - len(settled),
            "selected_n_total": len(selected), "selected_n_settled": len(selected_settled),
            "selected_n_pending": len(selected) - len(selected_settled),
            "selected_fraction_total": len(selected) / len(rows),
            "selected_fraction_settled": len(selected_settled) / len(settled) if settled else None,
        },
        "metrics": {
            subset: {model: metrics(records, "p_" + model)
                     for model in ("baseline", "candidate")}
            for subset, records in (("all_settled", settled), ("selected_settled", selected_settled))
        },
        "paired_bootstrap": paired_bootstrap(settled, bootstrap, seed),
        "limitations": [
            "Timestamp checks cannot establish authenticity, prevent undisclosed leakage, or verify held-out training.",
            "No sample-count gate or confidence interval automatically promotes a model or establishes highest accuracy.",
            "Week-cluster intervals assume weeks are resampling units; few weeks or cross-week dependence can impair coverage.",
            "Selected metrics apply only to the recorded selected subset; report coverage alongside accuracy.",
            "Coverage describes only the supplied file, not the complete universe of eligible matches.",
            "Captured timestamps do not establish source freshness or original prediction generation time.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Frozen prediction JSONL file")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bootstrap", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--horizon-tolerance-minutes", type=float, default=0,
                        help="Prespecified allowed lead-time deviation; default 0 (exact match)")
    args = parser.parse_args()
    try:
        result = evaluate(read_rows(args.input), args.bootstrap, args.seed,
                          args.horizon_tolerance_minutes)
        rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            if args.output.resolve() == args.input.resolve():
                raise ValueError("Output cannot overwrite the input predictions")
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
