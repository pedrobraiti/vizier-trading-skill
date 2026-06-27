"""Exposure reconciliation and position provenance (spec, §A + §B).

Two §B truths drive this module:

  * Broker positions lag the truth by ~30-45s. Reconciling a fresh buy against
    positions alone does NOT protect against the double-buy race it is supposed
    to prevent — the just-sent order is not visible yet. So we reconcile against
    the skill's OWN sent-order log (no lag) UNION the broker positions.

  * A Valet position carries no entry date and no reason. A position with no
    thesis record is therefore of UNKNOWN provenance: the skill must NOT
    silently apply hold-bias / anti-churn to it — it asks the user first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .constants import CRYPTO_POSITION_LAG_SECONDS, POSITION_LAG_SECONDS

# Statuses that mean the order will never create exposure, so it cannot cause a
# double-buy. Anything else (sent / pending / submitted / filled) still can.
TERMINAL_NON_EXPOSURE_STATUSES = frozenset(
    {"rejected", "cancelled", "canceled", "failed", "error", "expired"}
)


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _within_lag_window(order: dict[str, Any], now: datetime, lag_seconds: int) -> bool:
    """Is this order recent enough that its fill may not be in positions yet?
    An order with no usable timestamp is treated as recent (conservative)."""
    ts = _parse_timestamp(order.get("timestamp"))
    if ts is None:
        return True
    return (now - ts).total_seconds() <= lag_seconds


def reconcile_exposure(
    own_sent_orders: list[dict[str, Any]],
    broker_positions: list[dict[str, Any]],
    ticker: str,
    *,
    now: datetime | None = None,
    venue: str | None = None,
    lag_window_seconds: int | None = None,
) -> dict[str, Any]:
    """Reconcile real exposure for ``ticker`` against (own order log ∪ positions).

    ``own_sent_orders`` items: ``{ticker, side, value|qty, timestamp, status}``.
    ``broker_positions`` items: ``{ticker, quantity, market_value, ...}``.

    Flags ``double_buy_risk`` when a buy was sent for this ticker inside the lag
    window (so positions may not reflect it yet). In that case the skill must
    confirm the fill (``wait_for_fill`` on IBKR / poll ``order_status`` on crypto)
    before sending any second order for the same ticker — never fire blind into
    the lag. The ``recommended_action`` string is IBKR-flavored; on crypto read
    ``double_buy_risk`` and substitute the ``order_status`` poll.

    The lag window defaults per ``venue`` (crypto ~30s, else ~45s) unless an
    explicit ``lag_window_seconds`` is passed; the IBKR end is the conservative
    cross-venue default.
    """
    now = now or datetime.now(UTC)
    if lag_window_seconds is None:
        lag_window_seconds = (
            CRYPTO_POSITION_LAG_SECONDS
            if str(venue).lower() == "crypto"
            else POSITION_LAG_SECONDS
        )

    ticker_orders = [o for o in own_sent_orders if o.get("ticker") == ticker]
    sent_buys = [o for o in ticker_orders if str(o.get("side", "")).lower() == "buy"]
    active_buys = [
        o
        for o in sent_buys
        if str(o.get("status", "")).lower() not in TERMINAL_NON_EXPOSURE_STATUSES
    ]
    recent_buys = [o for o in active_buys if _within_lag_window(o, now, lag_window_seconds)]

    # Valet's execution server keys positions by ``symbol`` (its Position model
    # field), not ``ticker``. Read either so the broker-side cross-check actually
    # matches instead of silently degrading to own-log-only.
    broker_position = next(
        (p for p in broker_positions if (p.get("ticker") or p.get("symbol")) == ticker),
        None,
    )

    double_buy_risk = len(recent_buys) > 0
    if double_buy_risk:
        recommended_action = "wait_for_fill_before_new_order"
        reason = (
            f"{len(recent_buys)} buy order(s) for {ticker} sent within the "
            f"{lag_window_seconds}s lag window; positions may not reflect them yet"
        )
    else:
        recommended_action = "proceed"
        reason = f"no in-flight buys for {ticker} within the {lag_window_seconds}s lag window"

    return {
        "ticker": ticker,
        "venue": venue,
        "exposure_source": "own_order_log UNION broker_positions",
        "double_buy_risk": double_buy_risk,
        "recommended_action": recommended_action,
        "reason": reason,
        "recent_sent_buys": recent_buys,
        "sent_buys": sent_buys,
        "broker_position": broker_position,
        "lag_window_seconds": lag_window_seconds,
    }


def position_provenance(
    ticker: str,
    theses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify a held position by whether the skill has a thesis for it.

    Known  → the skill owns the thesis; horizon tags drive anti-churn correctly.
    Unknown → no record (bought manually, or on another machine). The skill must
    ask the user for intent/thesis before applying any hold-bias or churn rule;
    "broker is the truth" covers existence/size, never the why (spec, §A).
    """
    matches = [
        t
        for t in theses
        if t.get("ticker") == ticker and t.get("status", "open") == "open"
    ]
    if not matches:
        return {
            "ticker": ticker,
            "provenance": "unknown",
            "horizon_tag": "unknown",
            "horizon_tags": [],
            "apply_hold_bias": False,
            "action": "ask_user_for_intent",
            "message": (
                f"{ticker} has no thesis record - provenance unknown; do not apply "
                "hold-bias/anti-churn silently, ask the user for intent first"
            ),
        }

    tags = sorted({t.get("horizon_tag") for t in matches if t.get("horizon_tag")})
    return {
        "ticker": ticker,
        "provenance": "known",
        "horizon_tag": tags[0] if len(tags) == 1 else "mixed",
        "horizon_tags": tags,
        "apply_hold_bias": True,
        "action": "apply_horizon_rules",
        "message": f"{ticker} provenance known; horizon tag(s): {', '.join(tags)}",
    }
