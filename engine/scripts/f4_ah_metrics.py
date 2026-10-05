"""Pure offline direction diagnostics for home-perspective AH probabilities.

These descriptive metrics neither select a live wager nor establish a model
advantage.  Direction maximizes the probability of a positive return, separately
from EV.  Full and half wins count as positive returns; a push stays in the
denominator and is not a hit.  Recorded prices imply simulated unit-stake ROI,
not actual execution.  No fixture deduplication or eligibility cutoff is applied:
callers must supply one row per fixture in the intended comparison window.
"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from numbers import Integral, Real


CLASSES = ("full_win", "half_win", "push", "half_loss", "full_loss")
PROBABILITY_THRESHOLDS = (.55, .60, .65)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError("finite numeric value required")
    return float(value)


def _pair(value, *, score=False):
    if isinstance(value, Mapping):
        value = (value.get("home"), value.get("away"))
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("home/away pair required")
    if score:
        if any(isinstance(x, bool) or not isinstance(x, Integral) or x < 0 for x in value):
            raise ValueError("final score requires nonnegative integers")
        return tuple(int(x) for x in value)
    result = tuple(_number(x) for x in value)
    if any(x <= 1 for x in result):
        raise ValueError("decimal odds must be greater than one")
    return result


def _probabilities(value):
    if isinstance(value, Mapping):
        value = [value.get(name) for name in CLASSES]
    try:
        values = list(value)
    except TypeError:
        raise ValueError("five home-perspective settlement probabilities required") from None
    if len(values) != len(CLASSES):
        raise ValueError("five home-perspective settlement probabilities required")
    values = [_number(x) for x in values]
    if (any(x < 0 or x > 1 for x in values)
            or not math.isclose(sum(values), 1, abs_tol=1e-6, rel_tol=0)):
        raise ValueError("settlement probabilities must sum to one")
    return values


def _utc_kickoff(value):
    if value is None:
        return None, None
    if isinstance(value, datetime):
        kickoff = value
    elif isinstance(value, str) and "T" in value:
        try:
            kickoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid kickoff timestamp") from None
    else:
        raise ValueError("timezone-aware kickoff timestamp required")
    if kickoff.utcoffset() is None:
        raise ValueError("timezone-aware kickoff timestamp required")
    kickoff = kickoff.astimezone(timezone.utc)
    monday = kickoff.date() - timedelta(days=kickoff.weekday())
    return kickoff.isoformat(), monday.isoformat()


def _side_output(side, probabilities, price):
    returns = (price - 1, (price - 1) / 2, 0, -.5, -1)
    return {
        "side": side, "decimal_odds": price,
        "probabilities": dict(zip(CLASSES, probabilities)),
        "positive_return_probability": probabilities[0] + probabilities[1],
        "ev": sum(p * profit for p, profit in zip(probabilities, returns)),
    }


def _record(row, index, probabilities, settlement):
    if not isinstance(row, Mapping):
        raise ValueError("row must be a mapping")
    handicap = _number(row.get("home_handicap"))
    if not math.isclose(handicap * 4, round(handicap * 4), abs_tol=1e-9, rel_tol=0):
        raise ValueError("handicap must use quarter-goal increments")
    home_price, away_price = _pair(row.get("odds"))
    score = _pair(row.get("final_score"), score=True)
    p = _probabilities(probabilities)
    from f4_ah_research import _mask
    if any(not feasible and probability > 1e-6
           for probability, feasible in zip(p, _mask(handicap))):
        raise ValueError("probability assigned to an impossible settlement")
    kickoff, week = _utc_kickoff(row.get("kickoff_at"))
    match_id = row.get("match_id")
    if match_id is not None and (not isinstance(match_id, str) or not match_id.strip()):
        raise ValueError("match_id must be a nonempty string when supplied")
    options = {
        "home": _side_output("home", p, home_price),
        "away": _side_output("away", p[::-1], away_price),
    }
    side = ("home" if options["home"]["positive_return_probability"]
            >= options["away"]["positive_return_probability"] else "away")
    baseline_side = "home" if home_price <= away_price else "away"

    def outcome(chosen_side):
        option = options[chosen_side].copy()
        result = settlement(*score, handicap, chosen_side, option["decimal_odds"])
        option.update(settlement=result["settlement"], net_profit=result["net_profit"],
                      positive_return_hit=result["settlement"] in {"full_win", "half_win"})
        return option

    model, baseline = outcome(side), outcome(baseline_side)
    return {
        "row_index": index, "match_id": match_id, "kickoff_at": kickoff, "utc_week": week,
        "home_handicap": handicap, "final_score": list(score),
        "model_direction": model, "lower_price_heuristic_baseline": baseline,
        "paired_positive_return_hit_delta": int(model["positive_return_hit"])
        - int(baseline["positive_return_hit"]),
    }


def _summary(records, field):
    outcomes = [record[field] for record in records]
    count = len(outcomes)
    counts = Counter(outcome["settlement"] for outcome in outcomes)
    positive = counts["full_win"] + counts["half_win"]
    profit = sum(outcome["net_profit"] for outcome in outcomes)
    return {
        "count": count, "settlement_counts": {name: counts[name] for name in CLASSES},
        "positive_return_hits": positive,
        "positive_return_hit_rate": positive / count if count else None,
        "full_win_rate": counts["full_win"] / count if count else None,
        "simulated_net_profit": profit, "simulated_stake": float(count),
        "roi": profit / count if count else None,
        "average_price": sum(outcome["decimal_odds"] for outcome in outcomes) / count if count else None,
    }


def _group(records, valid_count):
    count = len(records)
    model = _summary(records, "model_direction")
    baseline = _summary(records, "lower_price_heuristic_baseline")
    return {
        "selected_count": count, "coverage": count / valid_count if valid_count else None,
        "coverage_denominator": valid_count,
        "model_direction": model, "lower_price_heuristic_baseline": baseline,
        "matched_rows": count,
        "paired_positive_return_hit_rate_delta": (
            sum(record["paired_positive_return_hit_delta"] for record in records) / count
            if count else None),
        "records": records, "actionable": False,
    }


def direction_metrics(rows, home_probabilities=None):
    """Score all valid rows and three fixed probability/EV diagnostics.

    Rows require ``home_handicap``, ``odds`` (home/away decimal pair), and
    ``final_score`` (home/away integer pair).  Probabilities are home-perspective
    five-vectors in ``CLASSES`` order or class mappings.  Supply an aligned matrix
    in ``home_probabilities``, or a ``probabilities`` field on every row.  Optional
    ``match_id``/aware ``kickoff_at`` are preserved for paired fixture/week work.

    Invalid rows are reported and excluded; matrix length mismatch raises rather
    than truncating a comparison.  Probability thresholds are predeclared, with
    nonnegative EV for the probability-chosen side.  The lower-price heuristic is
    evaluated on exactly that same subset and has no independent selection rule.
    Coverage uses all valid rows; input availability is reported separately.
    Ties choose home, both for the model direction and the price heuristic.
    """
    rows = list(rows)
    if home_probabilities is None:
        probabilities = [row.get("probabilities") if isinstance(row, Mapping) else None
                         for row in rows]
    else:
        probabilities = list(home_probabilities)
        if len(probabilities) != len(rows):
            raise ValueError("one probability vector per row required")
    records, rejected = [], []
    if rows:
        # Import only when settling: f4_ah_research can safely import this module
        # for scoring without an eager circular import or optimization dependency.
        from f4_ah_research import settle_asian_handicap
        for index, (row, p) in enumerate(zip(rows, probabilities)):
            try:
                records.append(_record(row, index, p, settle_asian_handicap))
            except (ValueError, TypeError, KeyError) as exc:
                rejected.append({"row_index": index, "reason": str(exc)})
    count = len(records)
    diagnostics = []
    for threshold in PROBABILITY_THRESHOLDS:
        selected = [record for record in records
                    if record["model_direction"]["positive_return_probability"] >= threshold
                    and record["model_direction"]["ev"] >= 0]
        diagnostics.append({"probability_threshold": threshold, "minimum_ev": 0.0,
                            **_group(selected, count)})
    return {
        "schema_version": "f4-ah-direction-metrics-v1", "input_rows": len(rows),
        "valid_rows": count, "rejected_rows": rejected,
        "valid_input_coverage": count / len(rows) if rows else None,
        "primary_metric": "positive_return_hit_rate",
        "direction_rule": "largest_positive_return_probability; home_on_ties",
        "positive_return_definition": "full_win_or_half_win; pushes_are_not_hits",
        "baseline_definition": "lower_decimal_price; home_on_ties; heuristic_not_calibrated_market_probabilities",
        "all_rows": _group(records, count),
        "probability_threshold_diagnostics": diagnostics,
        "actionable": False, "auto_promote": False,
    }


def paired_week_bootstrap(records, *, samples=2000, seed=0, confidence=.95):
    """Exploratory paired model-minus-price hit interval by UTC kickoff week.

    Takes any ``records`` list emitted by ``direction_metrics``.  Whole weeks are
    resampled, preserving model/baseline pairing and the fixture-weighted mean.
    A missing week on any record, zero fixtures, or fewer than two weeks yields
    no interval.  This interval does not by itself establish model superiority.
    """
    if isinstance(samples, bool) or not isinstance(samples, Integral) or samples < 1:
        raise ValueError("samples must be a positive integer")
    confidence = _number(confidence)
    if not 0 < confidence < 1:
        raise ValueError("confidence must be strictly between zero and one")
    records = list(records)
    groups = defaultdict(list)
    deltas, missing = [], 0
    for record in records:
        model = record["model_direction"]["positive_return_hit"]
        baseline = record["lower_price_heuristic_baseline"]["positive_return_hit"]
        if type(model) is not bool or type(baseline) is not bool:
            raise ValueError("paired hit values must be booleans")
        delta = int(model) - int(baseline)
        deltas.append(delta)
        week = record.get("utc_week")
        if week is None:
            missing += 1
        else:
            try:
                parsed_week = datetime.strptime(week, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                raise ValueError("UTC week must be its Monday date") from None
            if parsed_week.weekday() != 0 or parsed_week.isoformat() != week:
                raise ValueError("UTC week must be its Monday date")
            groups[week].append(delta)
    count = len(records)
    result = {
        "method": "paired_utc_week_cluster_bootstrap", "matched_rows": count,
        "utc_week_count": len(groups), "missing_utc_week_rows": missing,
        "paired_positive_return_hit_rate_delta": sum(deltas) / count if count else None,
        "confidence": confidence, "confidence_interval": None,
        "bootstrap_samples": 0, "requested_samples": int(samples), "seed": seed,
        "exploratory": True,
    }
    if not count:
        return {**result, "status": "empty"}
    if missing:
        return {**result, "status": "missing_utc_weeks"}
    if len(groups) < 2:
        return {**result, "status": "insufficient_utc_weeks"}
    blocks = [(sum(groups[week]), len(groups[week])) for week in sorted(groups)]
    generator = random.Random(seed)
    estimates = []
    for _ in range(samples):
        sampled = [generator.choice(blocks) for _ in blocks]
        estimates.append(sum(delta for delta, _ in sampled) / sum(n for _, n in sampled))
    estimates.sort()

    def quantile(fraction):
        index = (len(estimates) - 1) * fraction
        lower, upper = math.floor(index), math.ceil(index)
        return estimates[lower] + (estimates[upper] - estimates[lower]) * (index - lower)

    tail = (1 - confidence) / 2
    return {**result, "status": "exploratory_interval", "bootstrap_samples": int(samples),
            "confidence_interval": [quantile(tail), quantile(1 - tail)]}
