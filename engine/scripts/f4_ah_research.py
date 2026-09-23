"""Reproducible Asian-handicap shadow experiments; no betting or promotion.

All handicaps are from the home team's perspective (negative = home gives goals).
Prices are decimal, not Hong Kong water.  Provenance/timestamps are declarations
that callers must preserve and independently audit; this module cannot attest to
their truth.  Historical test scores are explicitly distinct from saved forecasts.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import logsumexp


SCHEMA_VERSION = "f4-ah-research-v1"
CLASSES = ("full_win", "half_win", "push", "half_loss", "full_loss")
CANDIDATES = ("M1", "M2", "M3")
REGULARIZATION_GRID = (0.1, 1.0, 10.0)
BASE_FEATURES = (
    "home_handicap", "absolute_handicap", "quarter_fraction", "home_log_price",
    "away_log_price", "ah_implied_home", "ah_overround", "log_p_home",
    "log_p_draw", "log_p_away", "handicap_x_log_home_away",
    "book_crown", "book_macau", "book_hkjc", "book_sbobet",
)
FEATURES = {
    "M1": BASE_FEATURES,
    "M2": BASE_FEATURES + ("rating_difference",),
    "M3": BASE_FEATURES + (
        "early_handicap", "handicap_change", "home_log_price_change",
        "away_log_price_change", "elapsed_quote_hours",
    ),
}


def _time(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and "T" in value:
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid timestamp") from None
    else:
        raise ValueError("explicit timezone-aware timestamp required")
    if result.utcoffset() is None:
        raise ValueError("timezone required")
    return result.astimezone(timezone.utc)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("finite numeric value required")
    return float(value)


def _line(value):
    result = _number(value)
    if not math.isclose(result * 4, round(result * 4), abs_tol=1e-9, rel_tol=0):
        raise ValueError("handicap must use quarter-goal increments")
    return result


def _price(value):
    value = _number(value)
    if value <= 1:
        raise ValueError("decimal odds must be greater than one")
    return value


def _pair(value, *, price=False, score=False):
    if isinstance(value, dict):
        value = (value.get("home"), value.get("away"))
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError("home/away pair required")
    if score:
        if any(type(x) is not int or x < 0 for x in value):
            raise ValueError("final score requires nonnegative integers")
        return tuple(value)
    return tuple((_price if price else _number)(x) for x in value)


def _provenance(value):
    if not isinstance(value, (str, dict)) or not value:
        raise ValueError("provenance required")
    if isinstance(value, str) and not value.strip():
        raise ValueError("provenance required")


def _probabilities(value):
    if isinstance(value, dict):
        value = [value.get(key) for key in ("home", "draw", "away")]
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("H/D/A probabilities required")
    values = [_number(x) for x in value]
    if any(x < 0 or x > 1 for x in values) or not math.isclose(sum(values), 1, abs_tol=1e-6):
        raise ValueError("H/D/A probabilities must sum to one")
    return values


def settle_asian_handicap(home_goals, away_goals, home_handicap, side, decimal_odds):
    """Settle a unit stake at any quarter-goal line, including arbitrarily deep lines."""
    home, away = _pair([home_goals, away_goals], score=True)
    handicap, odds = _line(home_handicap), _price(decimal_odds)
    if side not in {"home", "away"}:
        raise ValueError("side must be home or away")
    quarters = round(handicap * 4)
    # Odd quarters divide the stake across adjacent half-goal handicaps.
    legs = [quarters - 1, quarters + 1] if quarters % 2 else [quarters, quarters]
    signs = []
    for leg in legs:
        adjusted = 4 * (home - away) + leg
        adjusted *= 1 if side == "home" else -1
        signs.append(1 if adjusted > 0 else (-1 if adjusted < 0 else 0))
    grade = sum(signs)
    settlement = {2: "full_win", 1: "half_win", 0: "push",
                  -1: "half_loss", -2: "full_loss"}[grade]
    net = sum(odds - 1 if sign > 0 else (-1 if sign < 0 else 0) for sign in signs) / 2
    return {"settlement": settlement, "net_profit": net, "stake": 1.0,
            "home_handicap": handicap, "side": side, "decimal_odds": odds}


def _mask(handicap):
    # Only integer goal differences are possible.  Look around the line itself,
    # never around an arbitrarily truncated score grid (e.g. 0..6 goals).
    center = math.floor(-handicap)
    possible = set()
    for difference in range(center - 2, center + 4):
        possible.add(settle_asian_handicap(max(difference, 0), max(-difference, 0),
                                          handicap, "home", 2)["settlement"])
    return np.array([name in possible for name in CLASSES], dtype=bool)


def _validate_base(row, *, settled=False, now=None):
    if not isinstance(row, dict) or not isinstance(row.get("match_id"), str) or not row["match_id"]:
        raise ValueError("match identity required")
    observed, kickoff = _time(row.get("observed_at")), _time(row.get("kickoff_at"))
    if observed >= kickoff:
        raise ValueError("observation must precede kickoff")
    _line(row.get("home_handicap"))
    _pair(row.get("odds"), price=True)
    _probabilities(row.get("p_1x2"))
    _provenance(row.get("provenance"))
    if settled:
        finalized = _time(row.get("settled_at"))
        if finalized <= kickoff:
            raise ValueError("result availability must follow kickoff")
        if finalized > now:
            raise ValueError("result unavailable at research cutoff")
        _pair(row.get("final_score"), score=True)
    return observed, kickoff


def _features(row, candidate):
    if candidate not in CANDIDATES:
        raise ValueError("unknown candidate")
    observed, _ = _validate_base(row)
    h = _line(row["home_handicap"])
    oh, oa = _pair(row["odds"], price=True)
    ph, pd, pa = _probabilities(row["p_1x2"])
    logs = [math.log(max(x, 1e-8)) for x in (ph, pd, pa)]
    inverse = 1 / oh + 1 / oa
    values = [h, abs(h), h % 1, math.log(oh), math.log(oa),
              (1 / oh) / inverse, inverse - 1, *logs, h * (logs[0] - logs[2]),
              *[float(row.get('bookmaker') == name) for name in ('crown', 'macau', 'hkjc', 'sbobet')]]
    if candidate == "M2":
        ratings = row.get("ratings")
        if not isinstance(ratings, dict):
            raise ValueError("pre-match ratings required")
        if _time(ratings.get("observed_at")) > observed:
            raise ValueError("ratings newer than observation")
        _provenance(ratings.get("source"))
        home, away = _pair(ratings)
        values.append((home - away) / 400)
    if candidate == "M3":
        early = row.get("early_quote")
        if not isinstance(early, dict):
            raise ValueError("genuine earlier quote required")
        when = _time(early.get("observed_at"))
        if when >= observed:
            raise ValueError("early quote must be strictly earlier")
        _provenance(early.get("provenance"))
        if row.get('bookmaker') and early.get('bookmaker') != row['bookmaker']:
            raise ValueError('line movement requires the same bookmaker')
        early_line = _line(early.get("home_handicap"))
        eh, ea = _pair(early.get("odds"), price=True)
        values.extend([early_line, h - early_line, math.log(oh / eh),
                       math.log(oa / ea), (observed - when).total_seconds() / 3600])
    if not all(math.isfinite(x) for x in values):
        raise ValueError("non-finite feature")
    return values


def _matrix(rows, candidate):
    return np.array([_features(row, candidate) for row in rows], dtype=float)


def _labels(rows):
    return np.array([CLASSES.index(settle_asian_handicap(
        *_pair(row["final_score"], score=True), row["home_handicap"], "home",
        _pair(row["odds"], price=True)[0])["settlement"]) for row in rows], dtype=int)


def _probability_matrix(model, rows):
    raw = _matrix(rows, model["candidate_id"])
    means, scales = np.array(model["mean"]), np.array(model["scale"])
    coefficients = np.array(model["coefficients"])
    if (means.shape != (raw.shape[1],) or scales.shape != means.shape
            or coefficients.shape != (raw.shape[1] + 1, len(CLASSES))
            or not all(np.isfinite(x).all() for x in (means, scales, coefficients))
            or np.any(scales <= 0)):
        raise ValueError("invalid model coefficients or feature scaling")
    design = np.column_stack([np.ones(len(rows)), (raw - means) / scales])
    logits = design @ coefficients
    masks = np.stack([_mask(row["home_handicap"]) for row in rows])
    logits = np.where(masks, logits, -np.inf)
    return np.exp(logits - logsumexp(logits, axis=1, keepdims=True))


def _fit(rows, candidate, regularization):
    raw = _matrix(rows, candidate)
    mean, scale = raw.mean(axis=0), raw.std(axis=0)
    scale[scale < 1e-8] = 1
    design = np.column_stack([np.ones(len(rows)), (raw - mean) / scale])
    labels = _labels(rows)
    masks = np.stack([_mask(row["home_handicap"]) for row in rows])
    n, p = design.shape

    def objective(flat):
        coefficients = flat.reshape(p, len(CLASSES))
        logits = np.where(masks, design @ coefficients, -np.inf)
        normalizer = logsumexp(logits, axis=1)
        loss = np.mean(normalizer - logits[np.arange(n), labels])
        # Include a weak intercept prior: rare feasible classes cannot diverge.
        penalty = coefficients.copy()
        penalty[0] *= .01
        loss += regularization * np.sum(coefficients * penalty) / (2 * n)
        residual = np.exp(logits - normalizer[:, None])
        residual[np.arange(n), labels] -= 1
        gradient = (design.T @ residual + regularization * penalty) / n
        return float(loss), gradient.ravel()

    fit = minimize(objective, np.zeros(p * len(CLASSES)), method="L-BFGS-B", jac=True,
                   options={"maxiter": 600, "ftol": 1e-10})
    if not fit.success or not np.isfinite(fit.x).all():
        raise ValueError("optimizer did not converge")
    return {"candidate_id": candidate, "features": list(FEATURES[candidate]),
            "mean": mean.tolist(), "scale": scale.tolist(),
            "coefficients": fit.x.reshape(p, len(CLASSES)).tolist(),
            "regularization": regularization}


def _side_output(probabilities, price):
    p = [float(x) for x in probabilities]
    returns = [price - 1, (price - 1) / 2, 0, -.5, -1]
    return {"probabilities": dict(zip(CLASSES, p)),
            "ev": sum(x * y for x, y in zip(p, returns)), "decimal_odds": price,
            "positive_return_probability": p[0] + p[1]}


def predict(model, row):
    """Generate a shadow prediction only when the frozen model already existed."""
    observed, _ = _validate_base(row)
    if not isinstance(model, dict) or model.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported model schema")
    if _time(model.get("created_at")) > observed:
        raise ValueError("model did not exist at observation time")
    if _time(model.get("trained_through")) >= observed:
        raise ValueError("training/selection cutoff must precede observation")
    if model.get("features") != list(FEATURES.get(model.get("candidate_id"), ())):
        raise ValueError("model feature schema mismatch")
    p = _probability_matrix(model, [row])[0]
    oh, oa = _pair(row["odds"], price=True)
    return {"home": _side_output(p, oh), "away": _side_output(p[::-1], oa),
            "home_handicap": row["home_handicap"], "observed_at": row["observed_at"],
            "model_version": model["model_version"], "weight": 0,
            "selected": False, "status": "shadow_only"}


def _score(model, rows):
    probabilities = _probability_matrix(model, rows)
    labels = _labels(rows)
    one_hot = np.eye(len(CLASSES))[labels]
    metric = {
        "count": len(rows), "evaluation_kind": "historical_out_of_time_test",
        "prospective_evidence": False,
        "log_loss": float(-np.log(np.maximum(probabilities[np.arange(len(rows)), labels], 1e-15)).mean()),
        "brier": float(np.square(probabilities - one_hot).sum(axis=1).mean()),
        "settlement_accuracy": float(np.mean(probabilities.argmax(axis=1) == labels)),
        "settlement_counts": dict(Counter(CLASSES[i] for i in labels)),
    }
    # Predeclared diagnostic, never a promoted selection policy.
    selected, price_baseline, records = [], [], []
    for row, p in zip(rows, probabilities):
        oh, oa = _pair(row["odds"], price=True)
        options = {"home": _side_output(p, oh), "away": _side_output(p[::-1], oa)}
        side = max(("home", "away"), key=lambda s: options[s]["ev"])
        if options[side]["ev"] < .02:
            continue
        result = settle_asian_handicap(*_pair(row["final_score"], score=True),
                                       row["home_handicap"], side, oh if side == "home" else oa)
        baseline_side = "home" if oh <= oa else "away"
        baseline = settle_asian_handicap(*_pair(row["final_score"], score=True),
                                         row["home_handicap"], baseline_side,
                                         oh if baseline_side == "home" else oa)
        selected.append(result["net_profit"])
        price_baseline.append(baseline["net_profit"])
        records.append({"match_id": row["match_id"], "side": side,
                        "settlement": result["settlement"], "net_profit": result["net_profit"]})
    metric["ev_threshold_diagnostic"] = {
        "threshold": .02, "selected_count": len(selected),
        "coverage": len(selected) / len(rows), "simulated_net_profit": sum(selected),
        "simulated_roi": sum(selected) / len(selected) if selected else None,
        "positive_return_rate": sum(x > 0 for x in selected) / len(selected) if selected else None,
        "same_subset_lower_price_baseline_roi": sum(price_baseline) / len(selected) if selected else None,
        "records": records, "actionable": False,
    }
    return metric


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")).hexdigest()


def _split(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[_time(row["kickoff_at"])].append(row)
    times = sorted(groups)
    # Whole UTC kickoff batches stay together.  Boundaries are set before any
    # candidate eligibility, feature scaling, outcome inspection or tuning.
    a, b = max(1, int(len(times) * .6)), max(2, int(len(times) * .8))
    parts = {"train": [row for t in times[:a] for row in groups[t]],
             "validation": [row for t in times[a:b] for row in groups[t]],
             "test": [row for t in times[b:] for row in groups[t]]}
    purged = {"train": 0, "validation": 0}
    for name, next_name in (("train", "validation"), ("validation", "test")):
        if parts[next_name]:
            boundary = min(_time(r["observed_at"]) for r in parts[next_name])
            retained = [r for r in parts[name] if _time(r["settled_at"]) < boundary]
            purged[name] = len(parts[name]) - len(retained)
            parts[name] = retained
    return parts, purged


def research(rows, output_dir=None, now=None, min_train=120, min_validation=40, min_test=40):
    """Fit M1 market, M2 market+ratings, M3 market+real earlier-price movement.

    Requires one 90-minute result per match and strict availability timestamps.
    Uses chronological 60/20/20 kickoff batches, training-only feature scaling,
    validation-only regularization choice, then untouched final-test evaluation.
    Model creation time is the actual supplied research clock, not backdated.
    """
    now = _time(now) if now is not None else datetime.now(timezone.utc)
    minimums = {"train": min_train, "validation": min_validation, "test": min_test}
    if any(type(n) is not int or n < 1 for n in minimums.values()):
        raise ValueError("minimum sample sizes must be positive integers")
    rows = list(rows)
    rejected, valid = [], []
    for i, row in enumerate(rows):
        try:
            _validate_base(row, settled=True, now=now)
            valid.append(row)
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append({"index": i, "reason": str(exc)})
    by_id = defaultdict(list)
    for row in valid:
        by_id[row["match_id"]].append(row)
    unique, duplicates = [], 0
    for match_id, snapshots in by_id.items():
        signatures = {(_time(r["kickoff_at"]), _pair(r["final_score"], score=True)) for r in snapshots}
        if len(signatures) > 1:
            rejected.append({"match_id": match_id, "reason": "conflicting duplicate fixture/result"})
            continue
        latest = max(_time(r["observed_at"]) for r in snapshots)
        tied = [r for r in snapshots if _time(r["observed_at"]) == latest]
        if len({_digest(r) for r in tied}) > 1:
            rejected.append({"match_id": match_id, "reason": "conflicting simultaneous snapshots"})
            continue
        unique.append(tied[0])
        duplicates += len(snapshots) - 1
    unique.sort(key=lambda r: (_time(r["kickoff_at"]), r["match_id"]))
    parts, purged = _split(unique)
    report = {
        "schema_version": SCHEMA_VERSION, "status": "insufficient_data",
        "created_at": now.isoformat(), "weight": 0, "auto_promote": False,
        "input_rows": len(rows), "accepted_matches": len(unique), "rejected_rows": rejected,
        "duplicate_snapshots_removed": duplicates, "data_sha256": _digest(unique),
        "minimum_samples": minimums, "split": {"purged": purged}, "candidates": {},
        "limitations": ["provenance and availability declarations require independent audit",
                        "historical evaluation is not a saved prospective forecast",
                        "repeated testing of the same holdout cannot establish a fresh improvement",
                        "candidates remain zero-weight; no automatic promotion"],
        "selection_metric": "validation_settlement_log_loss",
        "regularization_grid": list(REGULARIZATION_GRID),
    }
    for name, part in parts.items():
        report["split"][name] = {"count": len(part), "match_ids": [r["match_id"] for r in part],
                                   "kickoff_from": part[0]["kickoff_at"] if part else None,
                                   "kickoff_to": part[-1]["kickoff_at"] if part else None}
    for candidate in CANDIDATES:
        eligible, missing = {}, {}
        for name, part in parts.items():
            eligible[name], missing[name] = [], []
            for row in part:
                try:
                    _features(row, candidate)
                    eligible[name].append(row)
                except (ValueError, TypeError, KeyError) as exc:
                    missing[name].append({"match_id": row["match_id"], "reason": str(exc)})
        result = {"status": "insufficient_data", "weight": 0, "reason": None,
                  "counts": {name: len(part) for name, part in eligible.items()},
                  "feature_names": list(FEATURES[candidate]), "missing_features": missing,
                  "model": None, "validation": None, "test": None}
        report["candidates"][candidate] = result
        if any(len(eligible[name]) < minimum for name, minimum in minimums.items()):
            result["reason"] = "insufficient chronological samples with required features"
            continue
        if len(set(_labels(eligible["train"]))) < 2:
            result["reason"] = "training outcomes lack class diversity"
            continue
        try:
            models = [_fit(eligible["train"], candidate, regularization)
                      for regularization in REGULARIZATION_GRID]
            validation = [_score(model, eligible["validation"]) for model in models]
            best = min(range(len(models)), key=lambda index: validation[index]["log_loss"])
            model = models[best]
            used = eligible["train"] + eligible["validation"]
            model.update({"schema_version": SCHEMA_VERSION, "created_at": now.isoformat(),
                          "trained_through": max(_time(r["settled_at"]) for r in used).isoformat(),
                          "training_data_sha256": _digest(eligible["train"]),
                          "selection_data_sha256": _digest(eligible["validation"]),
                          "weight": 0, "auto_promote": False, "classes": list(CLASSES)})
            model["model_version"] = candidate + "-" + _digest(model)[:16]
            result.update({"status": "trained_shadow", "model": model,
                           "validation": validation[best], "test": _score(model, eligible["test"]),
                           "regularization_search": [{"regularization": reg, "log_loss": score["log_loss"]}
                                                     for reg, score in zip(REGULARIZATION_GRID, validation)]})
            report["status"] = "trained_shadow"
        except (ValueError, FloatingPointError, OverflowError) as exc:
            result.update({"status": "fit_failed", "reason": str(exc)})
    # Serialize strictly before writing: NaN/Infinity cannot silently enter a ledger.
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if output_dir is not None:
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "report.json").write_text(serialized, encoding="utf-8")
    return report
