"""Validate saved AH probability evidence without fitting or changing records."""
from __future__ import annotations

import math


PROBABILITY_TOLERANCE = 1e-6
CONTRACT_TOLERANCE = 1e-9


def validate_saved_prediction(row, record):
    """Return ``(prediction, home_vector)`` or reject an inconsistent record.

    Missing ``hit_rate_direction`` permits historical probability diagnostics;
    it does not certify that the newer direction policy was saved prospectively.
    The caller must keep those two evidence types distinct.
    """
    # Lazy import avoids a cycle when research/reporting code uses this validator.
    from f4_ah_research import CLASSES, _line, _mask, _number, _pair, _price, _time

    try:
        if not isinstance(row, dict) or not isinstance(record, dict):
            raise ValueError("saved row and candidate record must be objects")
        observed, generated, kickoff = (_time(row.get(key)) for key in (
            "observed_at", "generated_at", "kickoff_at"))
        if not observed <= generated < kickoff:
            raise ValueError("saved prediction must be generated after observation and before kickoff")
        handicap = _line(row.get("home_handicap"))
        home_price, away_price = _pair(row.get("odds"), price=True)
        _pair(row.get("final_score"), score=True)

        prediction = record.get("prediction")
        if not isinstance(prediction, dict):
            raise ValueError("saved prediction required")
        version = record.get("model_version")
        if not isinstance(version, str) or not version.strip() or prediction.get("model_version") != version:
            raise ValueError("saved model version mismatch")
        if _time(prediction.get("observed_at")) != observed:
            raise ValueError("saved prediction observation mismatch")
        if not math.isclose(_line(prediction.get("home_handicap")), handicap,
                            abs_tol=CONTRACT_TOLERANCE, rel_tol=0):
            raise ValueError("saved prediction handicap mismatch")

        vectors = {}
        for side, price in (("home", home_price), ("away", away_price)):
            output = prediction.get(side)
            if not isinstance(output, dict):
                raise ValueError("saved side output required")
            if not math.isclose(_price(output.get("decimal_odds")), price,
                                abs_tol=CONTRACT_TOLERANCE, rel_tol=0):
                raise ValueError("saved prediction price mismatch")
            probabilities = output.get("probabilities")
            if not isinstance(probabilities, dict) or set(probabilities) != set(CLASSES):
                raise ValueError("exactly five named settlement probabilities required")
            vector = [_number(probabilities[name]) for name in CLASSES]
            if (any(value < 0 or value > 1 for value in vector)
                    or not math.isclose(sum(vector), 1, abs_tol=PROBABILITY_TOLERANCE, rel_tol=0)):
                raise ValueError("saved settlement probabilities must be normalized")
            vectors[side] = vector
        home = vectors["home"]
        if any(not feasible and probability > PROBABILITY_TOLERANCE
               for probability, feasible in zip(home, _mask(handicap))):
            raise ValueError("saved probability assigned to an impossible settlement")
        if any(not math.isclose(first, second, abs_tol=PROBABILITY_TOLERANCE, rel_tol=0)
               for first, second in zip(vectors["away"], reversed(home))):
            raise ValueError("saved away probabilities must reverse the home probabilities")

        direction = "home" if home[0] + home[1] >= home[3] + home[4] else "away"
        for saved in (record, prediction):
            if "hit_rate_direction" in saved and saved["hit_rate_direction"] != direction:
                raise ValueError("saved hit-rate direction does not match the probability policy")
        return prediction, home
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("invalid saved prediction record") from exc
