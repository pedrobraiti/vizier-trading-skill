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
    # Return is NET, from that same P&L: 19.5 / (10 x 10) — not the gross 20% price move.
    assert row["return_pct"] == pytest.approx(19.5)
    assert row["price_return_pct"] == pytest.approx(20.0)
    assert row["benchmark_return_pct"] == pytest.approx(10.0)  # SPY 100 -> 110
    assert row["alpha_pct"] == pytest.approx(9.5)
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


def test_benchmark_truncated_at_end_yields_null_alpha_with_reason():
    """REGRESSION (audit 10/10 item 7): a benchmark series ending well BEFORE the
    thesis window's end backfilled its last close and the alpha was KEPT, comparing
    unequal windows. Same window or no alpha — the reason is named."""
    result = compute_scorecard(
        [_thesis()], prices={"AAA": 11.0}, benchmarks={"ibkr": SPY}, as_of="2026-07-15"
    )  # SPY ends 2026-06-01, 44 days before as_of
    row = result["theses"][0]
    assert row["alpha_pct"] is None
    assert row["benchmark_return_pct"] is None
    assert "unequal windows" in row["benchmark_note"]


def test_benchmark_with_a_stale_start_bar_yields_null_alpha():
    """A hole at the START (last close far before open_date) is the same unequal
    window as a truncated end."""
    gappy = {"symbol": "SPY", "series": [
        {"date": "2025-10-01", "close": 90.0},
        {"date": "2026-06-01", "close": 120.0},
    ]}
    result = compute_scorecard(
        [_thesis()], prices={"AAA": 11.0}, benchmarks={"ibkr": gappy}, as_of="2026-06-01"
    )
    row = result["theses"][0]
    assert row["alpha_pct"] is None
    assert "window start" in row["benchmark_note"]


def test_benchmark_covering_the_window_has_no_note():
    result = compute_scorecard(
        [_thesis()], prices={"AAA": 11.0}, benchmarks={"ibkr": SPY}, as_of="2026-06-01"
    )
    row = result["theses"][0]
    assert row["alpha_pct"] is not None
    assert row["benchmark_note"] is None


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


def test_thesis_with_a_dollar_qty_is_skipped_not_scored():
    """A legacy/hand-edited record holding DOLLARS in `qty` (IBKR reports a
    cash-quantity order's fill in dollars) would produce a P&L off by the share
    price. Name it in `skipped` — a fabricated number is worse than none."""
    bad = _thesis(ticker="AAPL", entry_price=317.25, qty=2.0, cash_qty=2.0)
    result = compute_scorecard([bad], prices={"AAPL": 320.0}, as_of="2026-06-01")
    assert result["theses"] == []
    assert "DOLLARS recorded as SHARES" in result["skipped"][0]["reason"]


def test_thesis_with_a_correct_fractional_qty_is_scored():
    """The correctly-recorded version of that same US$ buy scores normally."""
    good = _thesis(ticker="AAPL", entry_price=317.25, qty=0.0063, cash_qty=2.0)
    result = compute_scorecard([good], prices={"AAPL": 337.25}, as_of="2026-06-01")
    assert result["skipped"] == []
    assert result["theses"][0]["pnl"] == pytest.approx(0.0063 * 20.0)


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


# ── Audit 10/10 regressions: net alpha (item 7) and trim/scorecard (item 6) ────


def _audit_thesis(**changes):
    """The exact synthetic record from the audit's reproduction script."""
    record = {
        "ticker": "SYNTH", "horizon_tag": "tactical", "open_date": "2026-10-01",
        "entry_price": 10, "qty": 10, "cash_qty": 100, "conviction": 3,
        "thesis_one_liner": "Synthetic audit", "reason": "Synthetic audit",
        "main_risk": "Synthetic audit",
        "review_trigger": {"type": "date", "value": "2026-10-10"},
        "review_trigger_is_hard_stop": False, "baseline_snapshot": {},
    }
    record.update(changes)
    return record


FLAT_SPY = {"symbol": "SPY", "series": [
    {"date": "2026-10-01", "close": 100}, {"date": "2026-10-10", "close": 100},
]}


def test_audit_net_loss_never_shows_positive_alpha_from_gross_price():
    """REGRESSION (audit item 7, exact repro): entry 10, exit 12 (+20% price), net
    realized -1, SPY flat. Alpha used to read +20 from the gross price while the
    hit rate said 0%. Return and alpha now come from the same NET P&L."""
    thesis = _audit_thesis(
        status="closed", close_date="2026-10-10", exit_price=12, realized_pnl=-1
    )
    result = compute_scorecard([thesis], benchmarks={"ibkr": FLAT_SPY}, as_of="2026-10-10")
    row = result["theses"][0]
    assert row["pnl"] == pytest.approx(-1.0)
    assert row["return_pct"] == pytest.approx(-1.0)  # -1 / (10 x 10)
    assert row["alpha_pct"] == pytest.approx(-1.0)
    assert row["alpha_pct"] < 0
    assert row["price_return_pct"] == pytest.approx(20.0)  # kept, labelled as raw price
    assert result["overall"]["hit_rate_pct"] == pytest.approx(0.0)
    assert result["overall"]["avg_alpha_pct"] < 0


def test_open_thesis_return_includes_priced_trims_net_of_fees(memory_dir):
    """Open P&L = realized trims (net of their fees) + mark on the remainder,
    over the cost of the lot as opened."""
    from vizier import memory

    memory.write_thesis(_audit_thesis(), memory_dir=memory_dir)
    memory.reduce_thesis_qty(
        "SYNTH", "2026-10-01", 5, exit_price=11, fees=1, memory_dir=memory_dir
    )
    record = memory.read_thesis("SYNTH", "2026-10-01", memory_dir=memory_dir)
    result = compute_scorecard([record], prices={"SYNTH": 12}, as_of="2026-10-10")
    row = result["theses"][0]
    # realized (11-10)*5 - 1 = 4 ; unrealized (12-10)*5 = 10 ; invested 10*10 = 100
    assert row["realized_pnl"] == pytest.approx(4.0)
    assert row["pnl"] == pytest.approx(14.0)
    assert row["return_pct"] == pytest.approx(14.0)
    assert row["qty"] == pytest.approx(5.0)
    assert row["qty_opened"] == pytest.approx(10.0)
    overall = result["overall"]
    assert overall["realized_pnl"] == pytest.approx(4.0)
    assert overall["unrealized_pnl"] == pytest.approx(10.0)
    assert overall["open_invested"] == pytest.approx(50.0)  # what is still held


def test_audit_partial_sell_no_longer_rejected_as_unit_inconsistent(memory_dir):
    """REGRESSION (audit item 6, exact repro): buy 10 @ 10 for $100, trim 5. The
    trim kept cash_qty=100 so the scorecard rejected the thesis as 'qty 5 does not
    match cash_qty/entry_price = 10'. cash_qty now shrinks with qty. The audit's
    trim carried no exit price, so that leg's P&L is unknown: the thesis is named
    for THAT reason — never for a unit inconsistency, never with a guessed P&L."""
    from vizier import memory

    memory.write_thesis(_audit_thesis(), memory_dir=memory_dir)
    memory.reduce_thesis_qty("SYNTH", "2026-10-01", 5, memory_dir=memory_dir)
    record = memory.read_thesis("SYNTH", "2026-10-01", memory_dir=memory_dir)
    assert record["qty"] == pytest.approx(5.0)
    assert record["cash_qty"] == pytest.approx(50.0)
    assert record["qty_opened"] == pytest.approx(10.0)
    assert record["cash_qty_opened"] == pytest.approx(100.0)

    result = compute_scorecard([record], prices={"SYNTH": 12}, as_of="2026-10-10")
    reason = result["skipped"][0]["reason"]
    assert "not a usable share quantity" not in reason
    assert "without exit_price" in reason


def test_audit_reduce_to_zero_then_close_is_scored(memory_dir):
    """REGRESSION (audit item 6, exact repro): trim 5 + trim 5 + close at 12 with
    realized 20. qty 0 used to exclude the closed thesis ('non-positive qty')."""
    from vizier import memory

    memory.write_thesis(_audit_thesis(), memory_dir=memory_dir)
    memory.reduce_thesis_qty("SYNTH", "2026-10-01", 5, memory_dir=memory_dir)
    memory.reduce_thesis_qty("SYNTH", "2026-10-01", 5, memory_dir=memory_dir)
    memory.close_thesis(
        "SYNTH", "2026-10-01", close_date="2026-10-10", exit_price=12,
        realized_pnl=20, memory_dir=memory_dir,
    )
    result = compute_scorecard(
        memory.list_all_theses(memory_dir=memory_dir),
        benchmarks={"ibkr": FLAT_SPY}, as_of="2026-10-10",
    )
    assert result["skipped"] == []
    row = result["theses"][0]
    assert row["status"] == "closed"
    assert row["qty"] == 0.0
    assert row["invested"] == pytest.approx(100.0)
    assert row["pnl"] == pytest.approx(20.0)
    assert row["return_pct"] == pytest.approx(20.0)
    assert row["alpha_pct"] == pytest.approx(20.0)
    assert result["overall"]["hit_rate_pct"] == pytest.approx(100.0)


def test_priced_trims_to_zero_close_without_restating_pnl(memory_dir):
    """Fully sold through priced trims: close-thesis may omit exit_price and
    realized_pnl — they are derived exactly from the recorded partial exits."""
    from vizier import memory

    memory.write_thesis(_audit_thesis(), memory_dir=memory_dir)
    memory.reduce_thesis_qty(
        "SYNTH", "2026-10-01", 4, exit_price=11, fees=0.5, memory_dir=memory_dir
    )
    memory.reduce_thesis_qty(
        "SYNTH", "2026-10-01", 6, exit_price=9, fees=0.5, memory_dir=memory_dir
    )
    closed = memory.close_thesis(
        "SYNTH", "2026-10-01", close_date="2026-10-10", memory_dir=memory_dir
    )
    # (11-10)*4 - 0.5 + (9-10)*6 - 0.5 = 3.5 - 6.5 = -3.0
    assert closed["realized_pnl"] == pytest.approx(-3.0)
    assert closed["exit_price"] == pytest.approx(9.0)
    result = compute_scorecard([closed], benchmarks={"ibkr": FLAT_SPY}, as_of="2026-10-10")
    row = result["theses"][0]
    assert row["pnl"] == pytest.approx(-3.0)
    assert row["return_pct"] == pytest.approx(-3.0)
    assert row["alpha_pct"] == pytest.approx(-3.0)


def test_trim_bookkeeping_that_does_not_add_up_is_skipped():
    hand_edited = _thesis(
        qty=5.0, qty_opened=10.0,
        partial_exits=[{"qty": 2.0, "exit_price": 11.0, "fees": 0.0, "realized_pnl": 2.0}],
    )
    result = compute_scorecard([hand_edited], prices={"AAA": 12.0}, as_of="2026-06-01")
    assert result["theses"] == []
    assert "does not reconcile" in result["skipped"][0]["reason"]
