"""Behavioral tests for exposure reconciliation and position provenance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vizier import reconcile


def _now():
    return datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)


def test_double_buy_risk_when_recent_buy_not_in_positions():
    """A buy sent 10s ago is inside the 45s lag window and not yet in positions
    -> sending another buy now risks doubling, so the skill must wait for fill."""
    sent = [
        {
            "ticker": "AAA",
            "side": "buy",
            "value": 100,
            "status": "submitted",
            "timestamp": (_now() - timedelta(seconds=10)).isoformat(),
        }
    ]
    result = reconcile.reconcile_exposure(sent, broker_positions=[], ticker="AAA", now=_now())
    assert result["double_buy_risk"] is True
    assert result["recommended_action"] == "wait_for_fill_before_new_order"
    assert result["exposure_source"] == "own_order_log UNION broker_positions"


def test_no_double_buy_risk_when_order_is_old():
    """An order from 5 minutes ago is past the lag window — its fill would be
    reflected in positions by now, so there is no race."""
    sent = [
        {
            "ticker": "AAA",
            "side": "buy",
            "status": "filled",
            "timestamp": (_now() - timedelta(minutes=5)).isoformat(),
        }
    ]
    result = reconcile.reconcile_exposure(sent, broker_positions=[], ticker="AAA", now=_now())
    assert result["double_buy_risk"] is False
    assert result["recommended_action"] == "proceed"


def test_rejected_order_does_not_create_double_buy_risk():
    sent = [
        {
            "ticker": "AAA",
            "side": "buy",
            "status": "rejected",
            "timestamp": _now().isoformat(),
        }
    ]
    result = reconcile.reconcile_exposure(sent, broker_positions=[], ticker="AAA", now=_now())
    assert result["double_buy_risk"] is False


def test_reconcile_unions_broker_position():
    positions = [{"ticker": "AAA", "quantity": 3, "market_value": 300}]
    result = reconcile.reconcile_exposure([], positions, ticker="AAA", now=_now())
    assert result["broker_position"] == positions[0]
    assert result["double_buy_risk"] is False


def test_reconcile_matches_broker_position_keyed_by_symbol():
    """Valet's execution server keys positions by ``symbol`` (not ``ticker``).
    The broker-side cross-check must still match, or the 'own-log UNION broker
    positions' intent silently degrades to own-log-only."""
    positions = [{"symbol": "AAA", "quantity": 3, "market_value": 300}]
    result = reconcile.reconcile_exposure([], positions, ticker="AAA", now=_now())
    assert result["broker_position"] == positions[0]


def test_order_without_timestamp_is_treated_as_in_flight():
    sent = [{"ticker": "AAA", "side": "buy", "status": "submitted"}]
    result = reconcile.reconcile_exposure(sent, [], ticker="AAA", now=_now())
    assert result["double_buy_risk"] is True


def test_venue_selects_the_lag_window():
    """A buy 40s ago is inside IBKR's 45s window but OUTSIDE crypto's 30s one,
    so the venue default changes the verdict (explicit lag still overrides)."""
    sent = [
        {
            "ticker": "AAA",
            "side": "buy",
            "status": "submitted",
            "timestamp": (_now() - timedelta(seconds=40)).isoformat(),
        }
    ]
    ibkr = reconcile.reconcile_exposure(sent, [], ticker="AAA", now=_now())  # default 45s
    assert ibkr["double_buy_risk"] is True
    assert ibkr["lag_window_seconds"] == 45

    crypto = reconcile.reconcile_exposure(sent, [], ticker="AAA", now=_now(), venue="crypto")
    assert crypto["venue"] == "crypto"
    assert crypto["lag_window_seconds"] == 30
    assert crypto["double_buy_risk"] is False  # 40s > 30s crypto window

    # Explicit window wins over the venue default.
    forced = reconcile.reconcile_exposure(
        sent, [], ticker="AAA", now=_now(), venue="crypto", lag_window_seconds=60
    )
    assert forced["double_buy_risk"] is True


# ── Provenance ───────────────────────────────────────────────────────────────


def test_unknown_provenance_position_is_flagged():
    """A position with no thesis must NOT get hold-bias/anti-churn silently —
    the skill asks the user first (spec, §A)."""
    result = reconcile.position_provenance("AAA", theses=[])
    assert result["provenance"] == "unknown"
    assert result["horizon_tag"] == "unknown"
    assert result["apply_hold_bias"] is False
    assert result["action"] == "ask_user_for_intent"


def test_known_provenance_returns_horizon_tag():
    theses = [{"ticker": "AAA", "status": "open", "horizon_tag": "core"}]
    result = reconcile.position_provenance("AAA", theses=theses)
    assert result["provenance"] == "known"
    assert result["horizon_tag"] == "core"
    assert result["apply_hold_bias"] is True


def test_mixed_horizon_tags_reported():
    theses = [
        {"ticker": "AAA", "status": "open", "horizon_tag": "core"},
        {"ticker": "AAA", "status": "open", "horizon_tag": "tactical"},
    ]
    result = reconcile.position_provenance("AAA", theses=theses)
    assert result["horizon_tag"] == "mixed"
    assert set(result["horizon_tags"]) == {"core", "tactical"}


def test_closed_thesis_does_not_count_as_provenance():
    theses = [{"ticker": "AAA", "status": "closed", "horizon_tag": "core"}]
    result = reconcile.position_provenance("AAA", theses=theses)
    assert result["provenance"] == "unknown"
