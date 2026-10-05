"""Align an observed earlier AH price with the current settlement contract.

A representative line chosen by price balance can switch between alternatives.
Its price must not be compared with a different earlier handicap.  A declared
main-line move is retained separately only when both snapshots certify it.
"""
from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit


def _line(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not math.isclose(value * 4, round(value * 4), abs_tol=1e-9, rel_tol=0)):
        raise ValueError("finite quarter-goal handicap required")
    return float(value)


def _prices(value):
    if isinstance(value, dict):
        value = [value.get("home"), value.get("away")]
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(isinstance(x, bool) or not isinstance(x, (int, float))
                   or not math.isfinite(x) or x <= 1 for x in value)):
        raise ValueError("two decimal prices required")
    return [float(x) for x in value]


def _host(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source URL required")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("source URL host required")
    return parsed.hostname.casefold(), parsed.port


def _same_line(first, second):
    return math.isclose(first, second, abs_tol=1e-9, rel_tol=0)


def _certified_main(quote):
    return quote.get("provider_main") is True and quote.get("line_selection") == "provider_main"


def aligned_early_quote(early_observation, current_quote):
    """Return one comparable earlier contract, or ``None`` when unavailable.

    ``home_handicap`` and ``odds`` retain the earlier selected quote.  Movement
    features must use ``fixed_line_home_handicap`` and ``fixed_line_odds``, which
    refer to the current handicap observed in that same earlier snapshot.  The
    earlier observation time is never replaced with a supplier opening time.
    """
    try:
        if not isinstance(early_observation, dict) or not isinstance(current_quote, dict):
            return None
        earlier = early_observation.get("quote")
        if not isinstance(earlier, dict):
            return None
        for key in ("source", "bookmaker", "line_selection"):
            if (not isinstance(earlier.get(key), str) or not earlier[key].strip()
                    or earlier[key] != current_quote.get(key)):
                return None
        if _host(earlier.get("url")) != _host(current_quote.get("url")):
            return None

        observed = early_observation.get("observed_at")
        if not isinstance(observed, str):
            return None
        when = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        if when.utcoffset() is None:
            return None
        if current_quote.get("captured_at") is not None:
            captured_at = current_quote["captured_at"]
            if not isinstance(captured_at, str):
                return None
            captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            if captured.utcoffset() is None or when >= captured:
                return None

        target = _line(current_quote.get("home_handicap"))
        selected = _line(earlier.get("home_handicap"))
        selected_prices = _prices(earlier.get("odds"))
        _prices(current_quote.get("odds"))
        matches = []
        lines = earlier.get("lines", [])
        if not isinstance(lines, list):
            return None
        for item in lines:
            if not isinstance(item, dict):
                return None
            if _same_line(_line(item.get("home_handicap")), target):
                matches.append(_prices(item.get("odds")))
        if len(matches) > 1:
            return None
        if matches:
            fixed_prices = matches[0]
            if _same_line(selected, target) and fixed_prices != selected_prices:
                return None
        elif _same_line(selected, target):
            fixed_prices = selected_prices
        else:
            return None

        provenance = early_observation.get("provenance") or earlier.get("provenance")
        if (not isinstance(provenance, (str, dict)) or not provenance
                or (isinstance(provenance, str) and not provenance.strip())):
            return None
        main_available = _certified_main(earlier) and _certified_main(current_quote)
        result = {
            "observed_at": observed,
            "home_handicap": selected,
            "odds": selected_prices,
            "fixed_line_home_handicap": target,
            "fixed_line_odds": fixed_prices,
            "bookmaker": earlier["bookmaker"],
            "source": earlier["source"],
            "url": earlier["url"],
            "line_selection": earlier["line_selection"],
            "provider_main": earlier.get("provider_main") is True,
            "provenance": deepcopy(provenance),
            "main_handicap_change": target - selected if main_available else None,
            "main_handicap_change_available": main_available,
        }
        for key in ("captured_at", "published_at", "snapshot_at", "provider_event_id", "content_sha256"):
            if key in earlier:
                result[key] = deepcopy(earlier[key])
        return result
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
