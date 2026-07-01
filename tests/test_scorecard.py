import pytest

from vizier.scorecard import compute_scorecard

SPY = {
    "symbol": "SPY",
    "series": [
        {"date": "2026-01-02", "close": 100.0},
        {"date": "2026-03-02", "close": 110.0},
        {"date": "2026-06-01", "close": 120.0},
    ],
}


def _thesis(**overrides):
    base = {
        "ticker": "AAA",
        "horizon_tag": "core",
        "open_date": "2026-01-02",
        "entry_price": 10.0,
        "qty": 10.0,
        "conviction": 4,
        "status": "open",
    }
    base.update(overrides)
    return base


def test_closed_thesis_uses_recorded_realized_pnl_and_computes_alpha():
    thesis = _thesis(
        status="closed", close_date="2026-03-02", exit_price=12.0, realized_pnl=19.5
    )
    result = compute_scorecard(
        [thesis], benchmarks={"ibkr": SPY}, as_of="2026-06-01"
    )
    row = result["theses"][0]
    assert row["pnl"] == 19.5  # the recorded number (fees included) wins over price diff
    assert row["return_pct"] == pytest.approx(20.0)
    assert row["benchmark_return_pct"] == pytest.approx(10.0)  # SPY 100 -> 110
    assert row["alpha_pct"] == pytest.approx(10.0)
    assert result["overall"]["closed_count"] == 1
    assert result["overall"]["hit_rate_pct"] == pytest.approx(100.0)


def test_open_thesis_marks_to_passed_price():
    result = compute_scorecard(
        [_thesis()], prices={"AAA": 9.0}, benchmarks={"ibkr": SPY}, as_of="2026-06-01"
    )
    row = result["theses"][0]
    assert row["pnl"] == pytest.approx(-10.0)  # (9 - 10) * 10
    assert row["return_pct"] == pytest.approx(-10.0)
    assert row["benchmark_return_pct"] == pytest.approx(20.0)  # SPY 100 -> 120
    assert row["alpha_pct"] == pytest.approx(-30.0)
    assert result["overall"]["unrealized_pnl"] == pytest.approx(-10.0)


def test_open_thesis_without_price_is_skipped_and_named():
    result = compute_scorecard([_thesis()], prices={}, as_of="2026-06-01")
    assert result["theses"] == []
    assert result["skipped"][0]["ticker"] == "AAA"
    assert "no current price" in result["skipped"][0]["reason"]


def test_benchmark_window_not_covered_yields_null_alpha_with_reason():
    thesis = _thesis(open_date="2025-06-01")  # before the SPY series starts
    result = compute_scorecard(
        [thesis], prices={"AAA": 11.0}, benchmarks={"ibkr": SPY}, as_of="2026-06-01"
    )
    row = result["theses"][0]
    assert row["alpha_pct"] is None
    assert "does not cover" in row["benchmark_note"]


def test_crypto_ticker_routes_to_crypto_benchmark():
    btc_bench = {
        "symbol": "BTC/USDT",
        "series": [
            {"date": "2026-01-02", "close": 50000.0},
            {"date": "2026-06-01", "close": 55000.0},
        ],
    }
    thesis = _thesis(ticker="ETH/USDT", entry_price=2000.0, qty=0.5)
    result = compute_scorecard(
        [thesis],
        prices={"ETH/USDT": 2400.0},
        benchmarks={"ibkr": SPY, "crypto": btc_bench},
        as_of="2026-06-01",
    )
    row = result["theses"][0]
    assert row["venue"] == "crypto"
    assert row["benchmark_symbol"] == "BTC/USDT"
    assert row["benchmark_return_pct"] == pytest.approx(10.0)
    assert row["alpha_pct"] == pytest.approx(20.0 - 10.0)


def test_hit_rate_and_win_loss_profile():
    winner = _thesis(status="closed", close_date="2026-02-01", exit_price=12.0,
                     realized_pnl=20.0)
    loser = _thesis(ticker="BBB", status="closed", close_date="2026-02-01",
                    exit_price=9.0, realized_pnl=-10.0)
    result = compute_scorecard([winner, loser], as_of="2026-06-01")
    overall = result["overall"]
    assert overall["closed_count"] == 2
    assert overall["hit_rate_pct"] == pytest.approx(50.0)
    assert overall["realized_pnl"] == pytest.approx(10.0)
    assert overall["win_loss_ratio"] == pytest.approx(2.0)


def test_empty_store_yields_nulls_not_zeros():
    result = compute_scorecard([], as_of="2026-06-01")
    assert result["overall"]["hit_rate_pct"] is None
    assert result["overall"]["realized_pnl"] is None


def test_thesis_without_qty_is_skipped():
    result = compute_scorecard([_thesis(qty=None)], prices={"AAA": 11.0}, as_of="2026-06-01")
    assert result["theses"] == []
    assert "entry_price or qty" in result["skipped"][0]["reason"]


def test_activity_summary_from_decision_log():
    log = [
        {"intent": "research"},
        {"intent": "buy AAA", "executed_orders": [
            {"side": "BUY", "ticker": "AAA", "value": 100.0},
            {"side": "SELL", "ticker": "BBB", "value": 40.0},
        ]},
    ]
    result = compute_scorecard([], as_of="2026-06-01", decision_log=log)
    assert result["activity"] == {
        "decisions": 2,
        "decisions_with_executions": 1,
        "executed_orders": 2,
        "buy_value_total": 100.0,
    }


def test_bad_as_of_raises():
    with pytest.raises(ValueError, match="as_of"):
        compute_scorecard([], as_of="not-a-date")
