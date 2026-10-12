"""Deterministic NAV per venue, with an explicit currency on every holding.

NAV is the denominator of every limit, size and autonomy ceiling, so it must
never be a guess. The two venues report it differently:

* **IBKR** — ``account_summary.net_liquidation`` is the broker's own valuation:
  pass it as ``reported_nav`` (with ``reported_currency``) and it is validated
  and, if it is in another currency than the NAV currency, converted.
* **crypto** — the Valet returns ``net_liquidation = null`` (a full valuation
  would need every asset priced), ``available_funds`` = FREE quote cash only,
  and ``positions`` = base assets with a quantity and no price. The NAV is
  therefore COMPUTED here from explicit cash balances + priced holdings.

Honesty rules (the valuation is REFUSED rather than approximated):
  * every holding carries its own ``price`` AND ``price_currency``; a holding
    with no price is never valued at 0 or skipped — it is listed as missing;
  * a currency other than the NAV currency needs an explicit ``fx_rates`` entry
    (NAV-currency units per 1 unit). No peg is assumed — not even USDT = USD;
  * crypto ``available_funds`` excludes quote cash locked by open BUY orders, so
    a computed NAV requires ``cash_includes_reserved=True`` — the caller's
    statement that the cash lines include it (``open_orders``: remaining x limit);
  * any missing input raises ``IncompleteValuationError`` listing ALL of them;
    a malformed one (NaN, negative price, …) raises ``ValueError``; a total that
    is not finite and positive is refused.
"""

from __future__ import annotations

from typing import Any

from .validation import finite_number, non_negative_number, positive_number, valid_nav

VENUES = ("ibkr", "crypto")


class IncompleteValuationError(ValueError):
    """Some input the valuation needs is absent; ``missing`` names each one."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        super().__init__(
            "valuation REFUSED as incomplete - a missing price or FX rate is never "
            "treated as 0 or skipped. Missing: " + "; ".join(self.missing)
        )


def _normalize_currency(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def _parse_fx_rates(fx_rates: dict[str, Any] | None, nav_currency: str) -> dict[str, float]:
    """Validate the FX table. The NAV currency converts to itself at exactly 1."""
    rates: dict[str, float] = {nav_currency: 1.0}
    for raw_currency, raw_rate in (fx_rates or {}).items():
        currency = _normalize_currency(raw_currency)
        if currency is None:
            raise ValueError("fx_rates has an empty currency key")
        rate = positive_number(raw_rate, f"fx_rates[{currency}]")
        if currency == nav_currency and rate != 1.0:
            raise ValueError(
                f"fx_rates[{currency}] must be 1 - it is the NAV currency itself, got {rate:g}"
            )
        rates[currency] = rate
    return rates


def _rate_or_missing(
    currency: str, rates: dict[str, float], missing: list[str], context: str
) -> float | None:
    rate = rates.get(currency)
    if rate is None:
        nav_currency = next(iter(rates))
        missing.append(
            f"fx rate {currency}->{nav_currency} (needed for {context}; no peg is assumed)"
        )
    return rate


def _value_cash(
    cash: list[dict[str, Any]], rates: dict[str, float], missing: list[str]
) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for index, line in enumerate(cash):
        currency = _normalize_currency(line.get("currency"))
        if currency is None:
            missing.append(f"currency of cash line #{index}")
            continue
        # Negative cash is allowed: a margin debit is a real liability of the account.
        amount = finite_number(line.get("amount"), f"cash[{currency}].amount")
        rate = _rate_or_missing(currency, rates, missing, f"cash {currency}")
        if rate is None:
            continue
        components.append(
            {
                "kind": "cash",
                "currency": currency,
                "amount": amount,
                "fx_rate": rate,
                "value": amount * rate,
            }
        )
    return components


def _value_holdings(
    holdings: list[dict[str, Any]], rates: dict[str, float], missing: list[str]
) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for index, holding in enumerate(holdings):
        symbol = holding.get("symbol")
        if not symbol:
            missing.append(f"symbol of holding #{index}")
            continue
        qty = non_negative_number(holding.get("qty"), f"holdings[{symbol}].qty")
        if qty == 0:
            continue  # an explicit zero balance contributes nothing; no price needed
        price_raw = holding.get("price")
        price_currency = _normalize_currency(holding.get("price_currency"))
        if price_raw is None:
            missing.append(f"price of {symbol}")
        if price_currency is None:
            missing.append(f"price_currency of {symbol}")
        if price_raw is None or price_currency is None:
            continue
        price = positive_number(price_raw, f"holdings[{symbol}].price")
        context = f"{symbol} priced in {price_currency}"
        rate = _rate_or_missing(price_currency, rates, missing, context)
        if rate is None:
            continue
        components.append(
            {
                "kind": "holding",
                "symbol": symbol,
                "qty": qty,
                "price": price,
                "price_currency": price_currency,
                "fx_rate": rate,
                "value": qty * price * rate,
            }
        )
    return components


def _require_valid_total(nav: float, nav_currency: str) -> float:
    try:
        return valid_nav(nav, "computed nav")
    except ValueError as exc:
        raise ValueError(
            f"valuation REFUSED: the NAV must be finite and positive ({exc}); "
            f"currency {nav_currency}"
        ) from None


def compute_nav(
    *,
    venue: str,
    currency: str,
    reported_nav: Any = None,
    reported_currency: str | None = None,
    cash: list[dict[str, Any]] | None = None,
    holdings: list[dict[str, Any]] | None = None,
    fx_rates: dict[str, Any] | None = None,
    cash_includes_reserved: bool = False,
) -> dict[str, Any]:
    """One venue's NAV in ``currency``, or a refusal — never an approximation.

    ``currency`` is the unit the NAV is expressed in; use the unit that venue's
    order amounts are in (IBKR: ``USD``; crypto: the quote currency from
    ``account_summary.currency``, e.g. ``USDT``) so sizes and NAV share a unit.

    Broker-reported mode — ``reported_nav`` (IBKR ``net_liquidation``) given:
    validated and, if ``reported_currency`` differs from ``currency``, converted
    by an explicit rate. ``cash``/``holdings`` must then be omitted.

    Computed mode — ``reported_nav`` null (crypto): ``cash`` =
    ``[{"currency", "amount"}]`` (TOTAL balances) and ``holdings`` =
    ``[{"symbol", "qty", "price", "price_currency"}]`` are both required (an
    empty list is an explicit "none"), plus ``cash_includes_reserved=True``.
    """
    if venue not in VENUES:
        raise ValueError(f"venue must be one of {VENUES}, got {venue!r}")
    nav_currency = _normalize_currency(currency)
    if nav_currency is None:
        raise ValueError("currency (the NAV's unit, e.g. USD or USDT) is required")
    rates = _parse_fx_rates(fx_rates, nav_currency)
    missing: list[str] = []

    if reported_nav is not None:
        if cash is not None or holdings is not None:
            raise ValueError(
                "pass reported_nav OR cash+holdings, not both - one valuation source per call"
            )
        amount = finite_number(reported_nav, "reported_nav")
        source_currency = _normalize_currency(reported_currency) or nav_currency
        rate = _rate_or_missing(source_currency, rates, missing, "the reported NAV")
        if missing:
            raise IncompleteValuationError(missing)
        nav = _require_valid_total(amount * rate, nav_currency)
        return {
            "venue": venue,
            "nav": nav,
            "currency": nav_currency,
            "source": "broker_reported",
            "components": [
                {
                    "kind": "reported_nav",
                    "currency": source_currency,
                    "amount": amount,
                    "fx_rate": rate,
                    "value": nav,
                }
            ],
        }

    if cash is None:
        missing.append(
            "cash balances (TOTAL per currency; pass [] if none) - the venue reported no NAV"
        )
    if holdings is None:
        missing.append(
            "holdings (every non-zero position with price and price_currency; pass [] if none)"
        )
    if not cash_includes_reserved:
        missing.append(
            "cash_includes_reserved=true - confirm the cash lines include quote cash locked "
            "by open BUY orders (crypto available_funds is FREE cash only; add remaining "
            "x limit price from open_orders)"
        )
    cash_components = _value_cash(cash or [], rates, missing)
    holding_components = _value_holdings(holdings or [], rates, missing)
    if missing:
        raise IncompleteValuationError(missing)

    cash_value = sum(component["value"] for component in cash_components)
    holdings_value = sum(component["value"] for component in holding_components)
    nav = _require_valid_total(cash_value + holdings_value, nav_currency)
    return {
        "venue": venue,
        "nav": nav,
        "currency": nav_currency,
        "source": "computed",
        "cash_value": cash_value,
        "holdings_value": holdings_value,
        "components": cash_components + holding_components,
    }
