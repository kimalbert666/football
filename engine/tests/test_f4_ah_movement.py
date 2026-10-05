"""Only synthetic snapshots; movement never compares different contracts."""
import copy
import math

import pytest

from f4_ah_movement import aligned_early_quote


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
