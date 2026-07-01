"""Behavioral tests for the persistent memory: thesis round-trip with the
baseline_snapshot preserved, tranche accounting, decision log, NAV drawdown,
and injectable (never-real) git commits."""

from __future__ import annotations

import json

import pytest

from vizier import memory


def _sample_thesis(ticker="ACME", open_date="2026-01-15", horizon_tag="core", qty=7):
    return {
        "ticker": ticker,
        "horizon_tag": horizon_tag,
        "open_date": open_date,
        "entry_price": 142.30,
        "qty": qty,
        "conviction": 4,
        "thesis_one_liner": "Quality compounder re-rating.",
        "reason": "Margin inflection underpriced.",
        "catalyst": "Q1 earnings",
        "catalyst_date": "2026-04-22",
        "main_risk": "Consumer slowdown delays re-rating.",
        "review_trigger": {"type": "price", "value": 120.0},
        "review_trigger_is_hard_stop": True,
        "baseline_snapshot": {
            "price": 142.30,
            "pe": 21.4,
            "rsi_14": 58,
            "vix": 16.2,
            "analyst_consensus": "buy",
        },
    }


# ── Thesis round-trip ────────────────────────────────────────────────────────


def test_thesis_write_read_roundtrip_preserves_baseline(memory_dir):
    memory.write_thesis(_sample_thesis(), memory_dir=memory_dir)
    loaded = memory.read_thesis("ACME", "2026-01-15", memory_dir=memory_dir)
    assert loaded["status"] == "open"
    # The baseline_snapshot is the load-bearing part — it must survive intact.
    assert loaded["baseline_snapshot"]["pe"] == 21.4
    assert loaded["baseline_snapshot"]["analyst_consensus"] == "buy"
    assert loaded["review_trigger"] == {"type": "price", "value": 120.0}


def test_thesis_missing_required_field_raises(memory_dir):
    bad = _sample_thesis()
    del bad["baseline_snapshot"]
    with pytest.raises(ValueError, match="baseline_snapshot"):
        memory.write_thesis(bad, memory_dir=memory_dir)


def test_thesis_invalid_horizon_tag_raises(memory_dir):
    bad = _sample_thesis(horizon_tag="swing")
    with pytest.raises(ValueError, match="horizon_tag"):
        memory.write_thesis(bad, memory_dir=memory_dir)


def test_thesis_missing_qty_raises(memory_dir):
    """qty is REQUIRED: tranche accounting sums it, so a null/missing qty would
    make the position invisible to the §D core-vs-tactical guard."""
    bad = _sample_thesis()
    del bad["qty"]
    with pytest.raises(ValueError, match="qty"):
        memory.write_thesis(bad, memory_dir=memory_dir)


def test_thesis_null_qty_raises(memory_dir):
    bad = _sample_thesis(qty=None)
    with pytest.raises(ValueError, match="qty"):
        memory.write_thesis(bad, memory_dir=memory_dir)


def test_crypto_ticker_slash_is_filename_safe(memory_dir):
    memory.write_thesis(
        _sample_thesis(ticker="BTC/USDT"), memory_dir=memory_dir
    )
    loaded = memory.read_thesis("BTC/USDT", "2026-01-15", memory_dir=memory_dir)
    assert loaded["ticker"] == "BTC/USDT"  # real symbol preserved inside the file


def test_close_thesis_preserves_baseline_and_adds_outcome(memory_dir):
    memory.write_thesis(_sample_thesis(), memory_dir=memory_dir)
    closed = memory.close_thesis(
        "ACME",
        "2026-01-15",
        close_date="2026-05-02",
        exit_price=171.2,
        realized_pnl=202.3,
        alpha_vs_spy=0.084,
        lesson="Re-rates on first guidance confirmation.",
        memory_dir=memory_dir,
    )
    assert closed["status"] == "closed"
    assert closed["realized_pnl"] == 202.3
    assert closed["baseline_snapshot"]["pe"] == 21.4  # still there after close


def test_list_open_theses_excludes_closed_and_examples(memory_dir):
    memory.write_thesis(_sample_thesis(ticker="AAA"), memory_dir=memory_dir)
    memory.write_thesis(_sample_thesis(ticker="BBB"), memory_dir=memory_dir)
    memory.close_thesis(
        "BBB", "2026-01-15", close_date="2026-02-01", exit_price=1, realized_pnl=0,
        memory_dir=memory_dir,
    )
    # An EXAMPLE_ template sitting in the dir must be ignored.
    (memory_dir / "theses" / "EXAMPLE_thesis.yaml").write_text(
        "ticker: EXAMPLE\nstatus: open\n", encoding="utf-8"
    )
    tickers = {t["ticker"] for t in memory.list_open_theses(memory_dir=memory_dir)}
    assert tickers == {"AAA"}


def test_update_last_reviewed(memory_dir):
    memory.write_thesis(_sample_thesis(), memory_dir=memory_dir)
    updated = memory.update_last_reviewed(
        "ACME", "2026-01-15", reviewed_at="2026-03-01", memory_dir=memory_dir
    )
    assert updated["last_reviewed"] == "2026-03-01"


# ── Tranche accounting (spec, §D) ────────────────────────────────────────────


def test_tranche_sell_cannot_eat_core(memory_dir):
    # Same ticker, two horizons: 10 core + 5 tactical.
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-01-01", horizon_tag="core", qty=10),
        memory_dir=memory_dir,
    )
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-02-01", horizon_tag="tactical", qty=5),
        memory_dir=memory_dir,
    )

    balances = memory.tranche_balances("ACME", memory_dir=memory_dir)
    assert balances == {"core": 10.0, "tactical": 5.0}

    # Selling 8 tactical would eat into core -> blocked.
    blocked = memory.check_tranche_sell("ACME", "tactical", 8, memory_dir=memory_dir)
    assert blocked["allowed"] is False
    assert blocked["available_in_tranche"] == 5.0

    # Selling the full 5 tactical is fine and leaves core untouched.
    ok = memory.check_tranche_sell("ACME", "tactical", 5, memory_dir=memory_dir)
    assert ok["allowed"] is True


# ── Decision log ─────────────────────────────────────────────────────────────


def test_append_decision_is_append_only_jsonl(memory_dir):
    memory.append_decision({"intent": "buy", "ticker": "AAA"}, memory_dir=memory_dir)
    memory.append_decision({"intent": "sell", "ticker": "BBB"}, memory_dir=memory_dir)
    entries = memory.read_decision_log(memory_dir=memory_dir)
    assert [e["ticker"] for e in entries] == ["AAA", "BBB"]
    assert all("timestamp" in e for e in entries)  # stamped automatically

    # Verify it really is one JSON object per line.
    raw = (memory_dir / memory.DECISION_LOG_NAME).read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["intent"] == "buy"


def test_read_decision_log_skips_one_corrupt_line(memory_dir):
    """A single torn/partial JSONL line must NOT hard-break reconstruction: the
    good lines still parse and aggregate (matching the Valet hardening)."""
    memory.append_decision({"intent": "buy", "ticker": "AAA"}, memory_dir=memory_dir)
    # Inject a corrupt line between two good ones.
    log_path = memory_dir / memory.DECISION_LOG_NAME
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json at all\n")
    memory.append_decision({"intent": "sell", "ticker": "BBB"}, memory_dir=memory_dir)

    entries = memory.read_decision_log(memory_dir=memory_dir)
    assert [e["ticker"] for e in entries] == ["AAA", "BBB"]


# ── reconcile-ready own_sent_orders from the decision log ────────────────────


def test_build_own_sent_orders_shapes_log_for_reconcile(memory_dir):
    """Each journaled executed order becomes a reconcile-ready row: timestamp
    falls back to the decision's, status to 'filled'. Filters by ticker."""
    memory.append_decision(
        {
            "timestamp": "2026-06-27T12:00:00+00:00",
            "intent": "buy AAA",
            "executed_orders": [
                {"side": "BUY", "ticker": "AAA", "value": 200.0, "order_id": "ib-1"},
                {"side": "BUY", "ticker": "BBB", "value": 50.0, "order_id": "ib-2"},
            ],
        },
        memory_dir=memory_dir,
    )
    rows = memory.build_own_sent_orders("AAA", memory_dir=memory_dir)
    assert rows["count"] == 1
    row = rows["own_sent_orders"][0]
    assert row["ticker"] == "AAA"
    assert row["timestamp"] == "2026-06-27T12:00:00+00:00"  # inherited from the decision
    assert row["status"] == "filled"  # a journaled order is a recorded fill
    assert row["order_id"] == "ib-1"


def test_build_own_sent_orders_within_seconds_filters_old(memory_dir):
    memory.append_decision(
        {
            "timestamp": "2026-06-27T12:00:00+00:00",
            "intent": "buy AAA",
            "executed_orders": [{"side": "BUY", "ticker": "AAA", "value": 200.0}],
        },
        memory_dir=memory_dir,
    )
    # 10 minutes later, a 60s window excludes the order.
    rows = memory.build_own_sent_orders(
        "AAA", now="2026-06-27T12:10:00+00:00", within_seconds=60, memory_dir=memory_dir
    )
    assert rows["count"] == 0


def test_build_own_sent_orders_feeds_reconcile(memory_dir):
    """End to end: the helper output drives reconcile to flag a recent buy."""
    from vizier import reconcile

    now = "2026-06-27T12:00:30+00:00"
    memory.append_decision(
        {
            "timestamp": "2026-06-27T12:00:00+00:00",  # 30s before `now` -> in 45s window
            "intent": "buy AAA",
            "executed_orders": [{"side": "BUY", "ticker": "AAA", "value": 200.0}],
        },
        memory_dir=memory_dir,
    )
    own = memory.build_own_sent_orders("AAA", now=now, memory_dir=memory_dir)["own_sent_orders"]
    result = reconcile.reconcile_exposure(
        own, broker_positions=[], ticker="AAA",
        now=memory._parse_dt(now),
    )
    assert result["double_buy_risk"] is True


# ── NAV snapshots + drawdown ─────────────────────────────────────────────────


def test_compute_drawdown_over_nav_series(memory_dir):
    for i, nav in enumerate([1_000, 1_100, 900, 950]):
        memory.record_nav_snapshot(
            nav, timestamp=f"2026-01-{i + 1:02d}T00:00:00+00:00", memory_dir=memory_dir
        )
    dd = memory.compute_drawdown(memory_dir=memory_dir)
    # Peak 1100 -> trough 900 = ~18.18% worst drawdown.
    assert dd["max_drawdown_pct"] == pytest.approx(200 / 1_100 * 100, rel=1e-6)
    # Latest 950 vs running peak 1100 = ~13.6% current drawdown.
    assert dd["current_drawdown_pct"] == pytest.approx(150 / 1_100 * 100, rel=1e-6)


def test_compute_drawdown_empty_series_is_zero(memory_dir):
    dd = memory.compute_drawdown(memory_dir=memory_dir)
    assert dd["max_drawdown_pct"] == 0.0
    assert dd["samples"] == 0


def test_compute_drawdown_refuses_mixed_venues_without_filter(memory_dir):
    # An interleaved IBKR($8)/crypto($1000) series reads as a phantom ~99% drawdown
    # that would trip the breaker on garbage — mixing venues must fail loudly.
    memory.record_nav_snapshot(8.0, timestamp="2026-01-01T00:00:00+00:00",
                               venue="ibkr", memory_dir=memory_dir)
    memory.record_nav_snapshot(1000.0, timestamp="2026-01-01T00:00:01+00:00",
                               venue="crypto", memory_dir=memory_dir)
    memory.record_nav_snapshot(7.5, timestamp="2026-01-02T00:00:00+00:00",
                               venue="ibkr", memory_dir=memory_dir)
    with pytest.raises(ValueError, match="multiple venues"):
        memory.compute_drawdown(memory_dir=memory_dir)


def test_compute_drawdown_filters_by_venue(memory_dir):
    memory.record_nav_snapshot(1000.0, timestamp="2026-01-01T00:00:00+00:00",
                               venue="crypto", memory_dir=memory_dir)
    memory.record_nav_snapshot(8.0, timestamp="2026-01-01T00:00:01+00:00",
                               venue="ibkr", memory_dir=memory_dir)
    memory.record_nav_snapshot(900.0, timestamp="2026-01-02T00:00:00+00:00",
                               venue="crypto", memory_dir=memory_dir)
    dd = memory.compute_drawdown(venue="crypto", memory_dir=memory_dir)
    assert dd["samples"] == 2
    assert dd["current_drawdown_pct"] == pytest.approx(10.0)


# ── Injectable git commits (tests never touch a real repo) ───────────────────


def test_commit_is_injectable_and_off_by_default(memory_dir):
    calls = []

    def fake_runner(args, cwd):
        calls.append((args, cwd))

    # commit=False (default): the runner is never invoked.
    memory.write_thesis(_sample_thesis(), memory_dir=memory_dir, runner=fake_runner)
    assert calls == []

    # commit=True: the runner is invoked, but it is our fake — no real git.
    memory.append_decision(
        {"intent": "buy"}, memory_dir=memory_dir, commit=True, runner=fake_runner
    )
    invoked = [args[0] for args, _ in calls]
    assert "init" in invoked and "add" in invoked and "commit" in invoked
    assert not (memory_dir / ".git").exists()  # nothing real was created


def test_commit_pushes_when_a_remote_exists(memory_dir):
    # The memory is the track record — once a private remote is attached, every
    # commit must also back itself up. Push is best-effort and remote-gated.
    class Result:
        def __init__(self, stdout=""):
            self.stdout = stdout

    calls = []

    def runner_with_remote(args, cwd):
        calls.append(args)
        return Result(stdout="origin\n" if args == ["remote"] else "")

    memory.commit_memory("backup", memory_dir=memory_dir, runner=runner_with_remote)
    assert ["push", "origin", "HEAD"] in calls


def test_commit_skips_push_without_a_remote(memory_dir):
    calls = []

    def runner_no_remote(args, cwd):
        calls.append(args)
        return None  # a bare fake: no stdout attribute at all

    memory.commit_memory("backup", memory_dir=memory_dir, runner=runner_no_remote)
    assert not any(args and args[0] == "push" for args in calls)
