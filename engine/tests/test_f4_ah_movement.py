"""Only synthetic snapshots; movement never compares different contracts."""
import copy
import math

import pytest

from f4_ah_movement import aligned_early_quote, observed_main_line_movement


def snapshots():
    earlier = {
        "observed_at": "2026-10-06T09:00:00Z",
        "provenance": {"kind": "synthetic-test-only"},
        "quote": {
            "bookmaker": "crown", "source": "infersports-crown",
            "url": "https://example.invalid/early", "content_sha256": "a" * 64,
            "captured_at": "2026-10-06T08:59:55Z",
            "line_selection": "closest_price_balance_not_provider_main",
            "home_handicap": -.25, "odds": [1.94, 1.96],
            "lines": [
                {"home_handicap": -.25, "odds": [1.94, 1.96]},
                {"home_handicap": -.5, "odds": [2.10, 1.80]},
            ],
        },
    }
    current = {
        "bookmaker": "crown", "source": "infersports-crown",
        "url": "https://example.invalid/current",
        "captured_at": "2026-10-06T11:45:00Z",
        "line_selection": "closest_price_balance_not_provider_main",
        "home_handicap": -.5, "odds": [1.96, 1.94],
    }
    return earlier, current


def test_balance_selected_switch_aligns_prices_and_preserves_earliest_observation():
    earlier, current = snapshots()
    before = copy.deepcopy(earlier), copy.deepcopy(current)
    result = aligned_early_quote(earlier, current)
    assert result["home_handicap"] == -.25
    assert result["odds"] == [1.94, 1.96]
    assert result["fixed_line_home_handicap"] == -.5
    assert result["fixed_line_odds"] == [2.10, 1.80]
    assert math.log(current["odds"][0] / result["fixed_line_odds"][0]) < 0
    assert math.log(current["odds"][1] / result["fixed_line_odds"][1]) > 0
    assert math.log(current["odds"][0] / result["odds"][0]) > 0
    assert math.log(current["odds"][1] / result["odds"][1]) < 0
    assert result["observed_at"] == earlier["observed_at"]
    assert result["captured_at"] == earlier["quote"]["captured_at"]
    assert result["provenance"] == earlier["provenance"]
    assert result["main_handicap_change"] is None
    assert result["main_handicap_change_available"] is False
    assert (earlier, current) == before
    result["provenance"]["kind"] = "changed-output"
    assert earlier == before[0]


def test_only_certified_provider_main_quotes_have_main_line_movement():
    earlier, current = snapshots()
    for quote in (earlier["quote"], current):
        quote.update(line_selection="provider_main", provider_main=True)
    result = aligned_early_quote(earlier, current)
    assert result["fixed_line_odds"] == [2.10, 1.80]
    assert result["main_handicap_change"] == -.25
    assert result["main_handicap_change_available"] is True


@pytest.mark.parametrize("flags,selection", [
    ((None, None), "provider_main"), ((True, False), "provider_main"),
    ((True, None), "provider_main"), ((True, True), "closest_price_balance_not_provider_main"),
    ((1, 1), "provider_main"),
])
def test_selection_label_or_one_certified_end_cannot_invent_main_movement(flags, selection):
    earlier, current = snapshots()
    for quote, flag in zip((earlier["quote"], current), flags):
        quote.update(line_selection=selection, provider_main=flag)
    result = aligned_early_quote(earlier, current)
    assert result is not None
    assert result["main_handicap_change"] is None
    assert result["main_handicap_change_available"] is False


def test_current_contract_not_observed_in_earlier_snapshot_is_unavailable():
    earlier, current = snapshots()
    earlier["quote"]["lines"] = earlier["quote"]["lines"][:1]
    assert aligned_early_quote(earlier, current) is None
    earlier["quote"].pop("lines")
    assert aligned_early_quote(earlier, current) is None


def test_same_selected_contract_can_be_used_without_alternate_lines():
    earlier, current = snapshots()
    earlier["quote"].pop("lines")
    current["home_handicap"] = -.25
    result = aligned_early_quote(earlier, current)
    assert result["fixed_line_odds"] == [1.94, 1.96]
    assert result["fixed_line_home_handicap"] == -.25


@pytest.mark.parametrize("mutation", [
    {"source": "other-provider"}, {"bookmaker": "hkjc"},
    {"url": "https://other.invalid/current"}, {"line_selection": "provider_main"},
    {"source": None}, {"url": "invalid"},
])
def test_incompatible_or_missing_quote_lineage_is_unavailable(mutation):
    earlier, current = snapshots()
    current.update(mutation)
    assert aligned_early_quote(earlier, current) is None


def test_duplicate_current_contract_is_ambiguous_even_with_identical_prices():
    earlier, current = snapshots()
    earlier["quote"]["lines"].append(copy.deepcopy(earlier["quote"]["lines"][1]))
    assert aligned_early_quote(earlier, current) is None


def test_selected_and_alternate_prices_cannot_conflict_for_same_contract():
    earlier, current = snapshots()
    current["home_handicap"] = -.25
    earlier["quote"]["lines"][0]["odds"] = [1.80, 2.10]
    assert aligned_early_quote(earlier, current) is None


@pytest.mark.parametrize("when", ["2026-10-06T09:00:00", "2026-10-06T11:45:00Z", "2026-10-06T12:00:00Z"])
def test_naive_or_not_strictly_earlier_observation_is_unavailable(when):
    earlier, current = snapshots()
    earlier["observed_at"] = when
    assert aligned_early_quote(earlier, current) is None


@pytest.mark.parametrize("mutation", [
    {"captured_at": 123}, {"home_handicap": True}, {"home_handicap": -.3},
    {"odds": [1.9, float("nan")]}, {"odds": [1, 2]},
])
def test_malformed_current_contract_is_unavailable(mutation):
    earlier, current = snapshots()
    current.update(mutation)
    assert aligned_early_quote(earlier, current) is None


def main_snapshots():
    earlier, current = snapshots()
    earlier.update(horizon="first-observed", kickoff_at="2026-10-06T12:00:00Z",
                   generated_at="2026-10-06T09:00:01Z")
    for quote in (earlier["quote"], current):
        quote.update(line_selection="provider_main", provider_main=True,
                     provider_event_id="synthetic-event", provider_kickoff_at=earlier["kickoff_at"],
                     market="90min_asian_handicap")
    earlier["quote"]["lines"] = earlier["quote"]["lines"][:1]
    return earlier, current


@pytest.mark.parametrize("initial,current_line", [(-.25, -.5), (-.5, -1), (.25, -.25), (-1, -.75)])
def test_main_shift_survives_missing_earlier_current_contract_without_cross_line_water(initial, current_line):
    earlier, current = main_snapshots()
    earlier["quote"]["home_handicap"] = initial
    earlier["quote"]["lines"] = [{"home_handicap": initial, "odds": [1.94, 1.96]}]
    current["home_handicap"] = current_line
    before = copy.deepcopy(earlier), copy.deepcopy(current)
    assert aligned_early_quote(earlier, current) is None
    result = observed_main_line_movement(earlier, current)
    assert result["earlier_home_handicap"] == initial
    assert result["current_home_handicap"] == current_line
    assert result["main_handicap_change"] == current_line - initial
    assert result["earlier_odds"] == [1.94, 1.96]
    assert result["current_odds"] == [1.96, 1.94]
    assert result["same_handicap_contract"] is False
    assert result["price_comparison_policy"] == "different_contract_prices_not_compared"
    assert "fixed_line_odds" not in result
    assert "home_log_price_change" not in result
    assert result["baseline_kind"] == "collector_first_observed"
    assert result["opening_certified"] is False
    assert (earlier, current) == before
    result["provenance"]["kind"] = "changed-output"
    result["earlier_odds"][0] = 10
    assert (earlier, current) == before


@pytest.mark.parametrize("horizon,kind", [("T-180m", "observed_T-180m"), ("T-60m", "observed_T-60m"),
                                        ("T-15m", "observed_T-15m"),
                                        ("T180", "observed_T180"), ("T60", "observed_T60"),
                                        ("T15", "observed_T15"), (None, "collector_earlier_observed")])
def test_main_movement_baseline_uses_actual_observation_kind_never_claimed_opening(horizon, kind):
    earlier, current = main_snapshots()
    earlier["horizon"] = horizon
    earlier["opening_certified"] = True  # An unsupported claim is never inherited.
    result = observed_main_line_movement(earlier, current)
    assert result["baseline_kind"] == kind
    assert result["opening_certified"] is False


def test_same_main_line_is_recorded_as_zero_shift_without_price_ratio():
    earlier, current = main_snapshots()
    current["home_handicap"] = earlier["quote"]["home_handicap"]
    result = observed_main_line_movement(earlier, current)
    assert result["main_handicap_change"] == 0
    assert result["same_handicap_contract"] is True


@pytest.mark.parametrize("mutation", [
    {"bookmaker": "hkjc"}, {"source": "different"}, {"url": "https://other.invalid/quote"},
    {"provider_main": False}, {"provider_main": 1},
    {"line_selection": "closest_price_balance_not_provider_main"},
    {"provider_event_id": "different-event"}, {"provider_event_id": None},
    {"provider_kickoff_at": "2026-10-06T12:15:00Z"}, {"provider_kickoff_at": None},
    {"captured_at": "2026-10-06T09:00:00Z"}, {"captured_at": "2026-10-06T08:00:00Z"},
    {"captured_at": "2026-10-06T12:00:00Z"}, {"captured_at": "2026-10-06T11:45:00"},
    {"published_at": "2026-10-06T11:45:01Z"}, {"market": "first_half_asian_handicap"},
    {"home_handicap": -.3}, {"home_handicap": True}, {"odds": [1.9, float("nan")]},
])
def test_main_movement_rejects_incompatible_identity_main_flags_time_and_contract(mutation):
    earlier, current = main_snapshots()
    current.update(mutation)
    assert observed_main_line_movement(earlier, current) is None


@pytest.mark.parametrize("key", ["provider_event_id", "provider_kickoff_at", "captured_at"])
def test_main_movement_one_end_missing_identity_or_current_time_is_unavailable(key):
    earlier, current = main_snapshots()
    current.pop(key)
    assert observed_main_line_movement(earlier, current) is None


@pytest.mark.parametrize("mutation", [
    {"observed_at": "2026-10-06T09:00:00"},
    {"observed_at": "2026-10-06T11:45:00Z"},
    {"generated_at": "2026-10-06T08:59:59Z"},
    {"generated_at": "2026-10-06T11:45:00Z"},
    {"kickoff_at": "2026-10-06T12:15:00Z"}, {"provenance": {}},
])
def test_main_movement_rejects_malformed_earlier_observation(mutation):
    earlier, current = main_snapshots()
    earlier.update(mutation)
    assert observed_main_line_movement(earlier, current) is None


def test_main_movement_rejects_earlier_capture_after_observation_or_future_published_value():
    earlier, current = main_snapshots()
    earlier["quote"]["captured_at"] = "2026-10-06T09:00:01Z"
    assert observed_main_line_movement(earlier, current) is None
    earlier["quote"]["captured_at"] = "2026-10-06T08:59:55Z"
    earlier["quote"]["published_at"] = "2026-10-06T09:00:00Z"
    assert observed_main_line_movement(earlier, current) is None


def test_main_movement_timezone_equivalent_kickoffs_match_and_neither_main_end_can_be_missing():
    earlier, current = main_snapshots()
    current["provider_kickoff_at"] = "2026-10-06T20:00:00+08:00"
    result = observed_main_line_movement(earlier, current)
    assert result["provider_event_id"] == "synthetic-event"
    earlier["quote"]["provider_main"] = False
    assert observed_main_line_movement(earlier, current) is None
