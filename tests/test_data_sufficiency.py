"""Behavioral tests for the data-sufficiency gate (spec, §B): a Scout
``{ok:true}`` with null fields must NOT pass as good data."""

from __future__ import annotations

from vizier import data_sufficiency


def test_valuation_full_evidence_proceeds():
    responses = {"price": 142.3, "pe": 21.4, "sector": "Tech"}
    result = data_sufficiency.assess(responses, "valuation")
    assert result["sufficient"] is True
    assert result["recommended_action"] == "proceed"


def test_valuation_null_price_abstains_even_with_others():
    # Rate-limited price feed: ok:true but price is None. Price is the anchor.
    responses = {"price": None, "pe": 21.4, "sector": "Tech"}
    result = data_sufficiency.assess(responses, "valuation")
    assert result["sufficient"] is False
    assert result["recommended_action"] == "abstain"
    assert "price" in result["missing"]


def test_valuation_missing_only_support_downsizes():
    # Anchor (price) present but no multiple and no sector -> downsize, not abstain.
    responses = {"price": 142.3, "pe": None, "sector": None}
    result = data_sufficiency.assess(responses, "valuation")
    assert result["sufficient"] is False
    assert result["recommended_action"] == "downsize"
    assert set(result["missing"]) == {"multiple", "sector"}


def test_valuation_any_multiple_satisfies():
    responses = {"price": 100, "ev_ebitda": 12, "sector": "Energy"}
    result = data_sufficiency.assess(responses, "valuation")
    assert result["sufficient"] is True


def test_valuation_pe_ratio_satisfies_multiple():
    """Scout's native field is ``pe_ratio`` (not ``pe``); it must count as a
    valuation multiple so a fully-fed Scout response proceeds."""
    responses = {"price": 142.3, "pe_ratio": 21.4, "sector": "Tech"}
    result = data_sufficiency.assess(responses, "valuation")
    assert result["sufficient"] is True
    assert result["recommended_action"] == "proceed"


def test_technical_needs_nonempty_price_history():
    empty = data_sufficiency.assess({"price_history": []}, "technical")
    assert empty["recommended_action"] == "abstain"

    full = data_sufficiency.assess({"price_history": [1, 2, 3]}, "technical")
    assert full["recommended_action"] == "proceed"


def test_unknown_decision_type_abstains():
    result = data_sufficiency.assess({"price": 1}, "astrology")
    assert result["recommended_action"] == "abstain"
    assert result["sufficient"] is False
