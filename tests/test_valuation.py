"""Deterministic NAV: complete valuations compute, incomplete ones are REFUSED.

Inputs mirror the real Valet payloads: the crypto ``account_summary`` returns
``net_liquidation: null``, ``available_funds`` = FREE quote cash and
``currency`` = the quote currency (``USDT``); ``positions`` lists base assets
(``{"symbol": "BTC", "quantity": "0.01"}``, decimals serialized as strings)
with no price. IBKR's ``account_summary`` carries ``net_liquidation`` in USD.
"""

from __future__ import annotations

import json
import math

import pytest

from vizier import cli
from vizier.valuation import IncompleteValuationError, compute_nav

# Real-shape crypto reads (Valet v0.8.0, decimals as strings).
CRYPTO_ACCOUNT_SUMMARY = {
    "account_id": "binance",
    "available_funds": "120.50",
    "net_liquidation": None,
    "buying_power": "120.50",
    "currency": "USDT",
}
CRYPTO_POSITIONS = [
    {"symbol": "BTC", "quantity": "0.01"},
    {"symbol": "ETH", "quantity": "0.5"},
]


def _crypto_holdings(prices: dict[str, float | None]) -> list[dict]:
    return [
        {
            "symbol": position["symbol"],
            "qty": position["quantity"],
            "price": prices.get(position["symbol"]),
            "price_currency": "USDT",
        }
        for position in CRYPTO_POSITIONS
    ]


def test_crypto_nav_computed_in_quote_currency():
    result = compute_nav(
        venue="crypto",
        currency=CRYPTO_ACCOUNT_SUMMARY["currency"],
        cash=[{"currency": "USDT", "amount": CRYPTO_ACCOUNT_SUMMARY["available_funds"]}],
        holdings=_crypto_holdings({"BTC": 60_000.0, "ETH": 3_000.0}),
        cash_includes_reserved=True,
    )
    assert result["source"] == "computed"
    assert result["currency"] == "USDT"
    assert result["holdings_value"] == pytest.approx(600.0 + 1_500.0)
    assert result["nav"] == pytest.approx(120.5 + 2_100.0)


def test_crypto_nav_in_usd_requires_explicit_usdt_rate():
    """No peg is assumed: USDT -> USD needs a rate, and every USDT line is listed."""
    with pytest.raises(IncompleteValuationError) as excinfo:
        compute_nav(
            venue="crypto",
            currency="USD",
            cash=[{"currency": "USDT", "amount": 120.5}],
            holdings=_crypto_holdings({"BTC": 60_000.0, "ETH": 3_000.0}),
            cash_includes_reserved=True,
        )
    missing = excinfo.value.missing
    assert len(missing) == 3  # the cash line + both holdings
    assert all("USDT->USD" in item for item in missing)

    result = compute_nav(
        venue="crypto",
        currency="USD",
        cash=[{"currency": "USDT", "amount": 120.5}],
        holdings=_crypto_holdings({"BTC": 60_000.0, "ETH": 3_000.0}),
        fx_rates={"USDT": 0.999},
        cash_includes_reserved=True,
    )
    assert result["nav"] == pytest.approx((120.5 + 2_100.0) * 0.999)


def test_missing_price_is_refused_not_valued_at_zero():
    with pytest.raises(IncompleteValuationError) as excinfo:
        compute_nav(
            venue="crypto",
            currency="USDT",
            cash=[{"currency": "USDT", "amount": 120.5}],
            holdings=_crypto_holdings({"BTC": 60_000.0}),  # ETH unpriced
            cash_includes_reserved=True,
        )
    assert excinfo.value.missing == ["price of ETH"]
    assert "never treated as 0" in str(excinfo.value)


def test_holding_without_price_currency_is_refused():
    with pytest.raises(IncompleteValuationError, match="price_currency of BTC"):
        compute_nav(
            venue="crypto",
            currency="USDT",
            cash=[],
            holdings=[{"symbol": "BTC", "qty": 0.01, "price": 60_000.0}],
            cash_includes_reserved=True,
        )


def test_computed_nav_requires_explicit_cash_holdings_and_reserved_confirmation():
    """Building a crypto NAV from available_funds alone would miss quote cash
    locked by open BUY orders; the caller must say the cash lines include it."""
    with pytest.raises(IncompleteValuationError) as excinfo:
        compute_nav(venue="crypto", currency="USDT")
    missing = " | ".join(excinfo.value.missing)
    assert "cash balances" in missing
    assert "holdings" in missing
    assert "cash_includes_reserved" in missing


def test_zero_quantity_holding_needs_no_price():
    result = compute_nav(
        venue="crypto",
        currency="USDT",
        cash=[{"currency": "USDT", "amount": 50}],
        holdings=[{"symbol": "DOGE", "qty": 0, "price": None, "price_currency": None}],
        cash_includes_reserved=True,
    )
    assert result["nav"] == pytest.approx(50.0)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [("price", math.nan), ("price", -1.0), ("price", 0.0), ("qty", -0.5), ("qty", math.inf)],
)
def test_malformed_holding_values_raise(field, bad_value):
    holding = {"symbol": "BTC", "qty": 0.01, "price": 60_000.0, "price_currency": "USDT"}
    holding[field] = bad_value
    with pytest.raises(ValueError):
        compute_nav(
            venue="crypto",
            currency="USDT",
            cash=[],
            holdings=[holding],
            cash_includes_reserved=True,
        )


@pytest.mark.parametrize("bad_rate", [math.nan, 0.0, -1.0, math.inf])
def test_malformed_fx_rate_raises(bad_rate):
    with pytest.raises(ValueError, match="fx_rates"):
        compute_nav(
            venue="crypto",
            currency="USD",
            cash=[{"currency": "USDT", "amount": 10}],
            holdings=[],
            fx_rates={"USDT": bad_rate},
            cash_includes_reserved=True,
        )


def test_non_positive_total_is_refused():
    with pytest.raises(ValueError, match="REFUSED"):
        compute_nav(
            venue="crypto",
            currency="USDT",
            cash=[{"currency": "USDT", "amount": 0}],
            holdings=[],
            cash_includes_reserved=True,
        )


def test_ibkr_reported_nav_is_validated_and_used():
    result = compute_nav(venue="ibkr", currency="USD", reported_nav="1234.56")
    assert result["source"] == "broker_reported"
    assert result["nav"] == pytest.approx(1234.56)


@pytest.mark.parametrize("bad_nav", [math.nan, math.inf, 0, -5])
def test_ibkr_reported_nav_non_finite_or_non_positive_is_refused(bad_nav):
    with pytest.raises(ValueError):
        compute_nav(venue="ibkr", currency="USD", reported_nav=bad_nav)


def test_reported_nav_in_other_currency_needs_a_rate():
    with pytest.raises(IncompleteValuationError, match="EUR->USD"):
        compute_nav(venue="ibkr", currency="USD", reported_nav=1000, reported_currency="EUR")
    result = compute_nav(
        venue="ibkr", currency="USD", reported_nav=1000, reported_currency="EUR",
        fx_rates={"EUR": 1.1},
    )
    assert result["nav"] == pytest.approx(1100.0)


def test_reported_and_components_together_is_ambiguous():
    with pytest.raises(ValueError, match="not both"):
        compute_nav(venue="ibkr", currency="USD", reported_nav=1000, cash=[])


def test_unknown_venue_raises():
    with pytest.raises(ValueError, match="venue"):
        compute_nav(venue="kraken", currency="USD", reported_nav=1000)


def _run(capsys, payload: str) -> tuple[int, dict]:
    code = cli.main(["nav", "--json", payload])
    return code, json.loads(capsys.readouterr().out.strip())


def test_cli_nav_refusal_lists_what_is_missing(capsys):
    payload = json.dumps(
        {
            "venue": "crypto",
            "currency": "USDT",
            "cash": [{"currency": "USDT", "amount": "120.50"}],
            "holdings": _crypto_holdings({"BTC": 60_000.0}),
            "cash_includes_reserved": True,
        }
    )
    code, envelope = _run(capsys, payload)
    assert code == 1
    assert envelope["ok"] is False
    assert envelope["error_type"] == "IncompleteValuationError"
    assert envelope["missing"] == ["price of ETH"]


def test_cli_nav_ok(capsys):
    payload = json.dumps({"venue": "ibkr", "currency": "USD", "reported_nav": 5000})
    code, envelope = _run(capsys, payload)
    assert code == 0
    assert envelope["data"]["nav"] == pytest.approx(5000.0)


def test_cli_rejects_nan_literal_in_payload(capsys):
    """REGRESSION (audit item 2): Python's json accepts NaN/Infinity by default."""
    code, envelope = _run(capsys, '{"venue": "ibkr", "currency": "USD", "reported_nav": NaN}')
    assert code == 1
    assert "non-finite" in envelope["error"]
