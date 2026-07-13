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


def test_thesis_qty_in_dollars_is_refused(memory_dir):
    """REGRESSION (live bug, 2026-07-13): `buy(AAPL, cash_amount=2)` filled 0.0063
    shares @ $317.25 and IBKR reported filled_quantity = 2.0 — the DOLLARS. Writing
    that as `qty` would corrupt the tranche guard, the P&L and the scorecard, so the
    store refuses the wrong UNIT at the door."""
    bad = _sample_thesis(qty=2.0)
    bad["entry_price"] = 317.25
    bad["cash_qty"] = 2.0  # the dollars deployed — qty == cash_qty is the signature
    with pytest.raises(ValueError, match="wrong UNIT"):
        memory.write_thesis(bad, memory_dir=memory_dir)


def test_thesis_qty_in_shares_with_cash_qty_is_accepted(memory_dir):
    """The CORRECT record for that same dollar buy: qty in shares, dollars in cash_qty."""
    good = _sample_thesis(ticker="AAPL", qty=0.0063)
    good["entry_price"] = 317.25
    good["cash_qty"] = 2.0
    memory.write_thesis(good, memory_dir=memory_dir)
    loaded = memory.read_thesis("AAPL", "2026-01-15", memory_dir=memory_dir)
    assert loaded["qty"] == pytest.approx(0.0063)
    assert loaded["cash_qty"] == 2.0
    # And tranche accounting now sees the real share count, not 2.0.
    assert memory.tranche_balances("AAPL", memory_dir=memory_dir)["core"] == pytest.approx(0.0063)


def test_thesis_qty_inconsistent_with_cash_and_price_is_refused(memory_dir):
    """Not just the exact dollars-as-shares signature: any qty that cannot be
    reconciled with cash_qty/entry_price is a mis-recorded fill."""
    bad = _sample_thesis(qty=5.0)
    bad["entry_price"] = 100.0
    bad["cash_qty"] = 2.0  # $2 at $100 cannot be 5 shares
    with pytest.raises(ValueError, match="inconsistent"):
        memory.write_thesis(bad, memory_dir=memory_dir)


def test_thesis_non_positive_qty_raises(memory_dir):
    with pytest.raises(ValueError, match="positive"):
        memory.write_thesis(_sample_thesis(qty=0), memory_dir=memory_dir)


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


# ── Same-day lots: no silent clobber (Z1) ────────────────────────────────────


def test_two_same_day_lots_coexist_and_tranches_sum_both(memory_dir):
    """REGRESSION: core qty=5 then tactical qty=3 on the SAME ticker+day used to
    collapse into ONE file ({ticker}-{open_date}.yaml overwritten silently) — the
    tranche guard then approved sells against a phantom balance missing the core."""
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="core", qty=5),
        memory_dir=memory_dir,
    )
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="tactical", qty=3),
        memory_dir=memory_dir,
    )
    files = sorted(p.name for p in (memory_dir / "theses").glob("ACME-2026-03-01*"))
    assert files == ["ACME-2026-03-01-tactical.yaml", "ACME-2026-03-01.yaml"]
    assert memory.tranche_balances("ACME", memory_dir=memory_dir) == {
        "core": 5.0,
        "tactical": 3.0,
    }


def test_write_thesis_never_lands_on_an_existing_file(memory_dir):
    """A duplicate write (same ticker/date/tag) must not clobber the stored lot:
    the first record survives byte-identical; the second lands on a new name."""
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="core", qty=5),
        memory_dir=memory_dir,
    )
    first = memory_dir / "theses" / "ACME-2026-03-01.yaml"
    original = first.read_text(encoding="utf-8")
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="core", qty=99),
        memory_dir=memory_dir,
    )
    assert first.read_text(encoding="utf-8") == original  # untouched
    assert (memory_dir / "theses" / "ACME-2026-03-01-core.yaml").exists()


def test_third_same_tag_lot_gets_numeric_suffix(memory_dir):
    for qty in (5, 6, 7):
        memory.write_thesis(
            _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="core", qty=qty),
            memory_dir=memory_dir,
        )
    assert (memory_dir / "theses" / "ACME-2026-03-01-core-2.yaml").exists()
    balances = memory.tranche_balances("ACME", memory_dir=memory_dir)
    assert balances["core"] == 18.0  # 5 + 6 + 7 all counted


def test_write_thesis_overwrite_updates_in_place(memory_dir):
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="core", qty=5),
        memory_dir=memory_dir,
    )
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="core", qty=8),
        overwrite=True,
        memory_dir=memory_dir,
    )
    files = list((memory_dir / "theses").glob("ACME-2026-03-01*"))
    assert len(files) == 1  # updated in place, no second lot
    assert memory.read_thesis("ACME", "2026-03-01", memory_dir=memory_dir)["qty"] == 8


def test_write_thesis_overwrite_without_existing_record_raises(memory_dir):
    with pytest.raises(FileNotFoundError, match="no thesis"):
        memory.write_thesis(
            _sample_thesis(ticker="ACME", open_date="2026-03-01"),
            overwrite=True,
            memory_dir=memory_dir,
        )


def test_read_thesis_ambiguous_same_day_requires_horizon_tag(memory_dir):
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="core", qty=5),
        memory_dir=memory_dir,
    )
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="tactical", qty=3),
        memory_dir=memory_dir,
    )
    with pytest.raises(ValueError, match="horizon_tag"):
        memory.read_thesis("ACME", "2026-03-01", memory_dir=memory_dir)
    tactical = memory.read_thesis(
        "ACME", "2026-03-01", horizon_tag="tactical", memory_dir=memory_dir
    )
    assert tactical["qty"] == 3


def test_close_thesis_disambiguates_by_horizon_tag(memory_dir):
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="core", qty=5),
        memory_dir=memory_dir,
    )
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="tactical", qty=3),
        memory_dir=memory_dir,
    )
    with pytest.raises(ValueError, match="horizon_tag"):
        memory.close_thesis(
            "ACME", "2026-03-01", close_date="2026-03-10", exit_price=150.0,
            realized_pnl=20.0, memory_dir=memory_dir,
        )
    closed = memory.close_thesis(
        "ACME", "2026-03-01", close_date="2026-03-10", exit_price=150.0,
        realized_pnl=20.0, horizon_tag="tactical", memory_dir=memory_dir,
    )
    assert closed["horizon_tag"] == "tactical"
    # The core lot is untouched and still open.
    assert memory.tranche_balances("ACME", memory_dir=memory_dir) == {
        "core": 5.0,
        "tactical": 0.0,
    }


def test_legacy_base_filename_still_read_and_closed(memory_dir):
    """Backward compat: files written by the old fixed-path scheme keep working."""
    legacy = memory_dir / "theses" / "OLDCO-2026-01-10.yaml"
    record = _sample_thesis(ticker="OLDCO", open_date="2026-01-10", qty=4)
    record["status"] = "open"
    import yaml as _yaml

    legacy.write_text(_yaml.safe_dump(record, sort_keys=False), encoding="utf-8")
    assert memory.read_thesis("OLDCO", "2026-01-10", memory_dir=memory_dir)["qty"] == 4
    closed = memory.close_thesis(
        "OLDCO", "2026-01-10", close_date="2026-02-01", exit_price=1.0,
        realized_pnl=0.0, memory_dir=memory_dir,
    )
    assert closed["status"] == "closed"


# ── reduce_thesis_qty: partial-sell bookkeeping (Z2) ─────────────────────────


def test_reduce_thesis_qty_decrements_and_updates_tranche(memory_dir):
    """REGRESSION: after a trim there was NO way to decrement the thesis qty, so
    the tranche guard kept approving sells against the pre-trim phantom balance."""
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="tactical", qty=5),
        memory_dir=memory_dir,
    )
    result = memory.reduce_thesis_qty("ACME", "2026-03-01", 2, memory_dir=memory_dir)
    assert result["previous_qty"] == 5.0
    assert result["remaining_qty"] == 3.0
    assert memory.tranche_balances("ACME", memory_dir=memory_dir)["tactical"] == 3.0
    # The guard now blocks a sell that the phantom balance would have approved.
    assert memory.check_tranche_sell("ACME", "tactical", 4, memory_dir=memory_dir)[
        "allowed"
    ] is False


def test_reduce_thesis_qty_to_zero_points_to_close_thesis(memory_dir):
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", qty=5),
        memory_dir=memory_dir,
    )
    result = memory.reduce_thesis_qty("ACME", "2026-03-01", 5, memory_dir=memory_dir)
    assert result["remaining_qty"] == 0.0
    assert "close-thesis" in result["note"]
    # NOT auto-closed: exit_price/realized_pnl belong to close_thesis.
    assert memory.read_thesis("ACME", "2026-03-01", memory_dir=memory_dir)["status"] == "open"


def test_reduce_thesis_qty_below_zero_raises(memory_dir):
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", qty=5),
        memory_dir=memory_dir,
    )
    with pytest.raises(ValueError, match="below zero"):
        memory.reduce_thesis_qty("ACME", "2026-03-01", 6, memory_dir=memory_dir)


def test_reduce_thesis_qty_rejects_non_positive_sale(memory_dir):
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", qty=5),
        memory_dir=memory_dir,
    )
    with pytest.raises(ValueError, match="positive"):
        memory.reduce_thesis_qty("ACME", "2026-03-01", 0, memory_dir=memory_dir)


def test_reduce_thesis_qty_disambiguates_by_horizon_tag(memory_dir):
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="core", qty=10),
        memory_dir=memory_dir,
    )
    memory.write_thesis(
        _sample_thesis(ticker="ACME", open_date="2026-03-01", horizon_tag="tactical", qty=5),
        memory_dir=memory_dir,
    )
    memory.reduce_thesis_qty(
        "ACME", "2026-03-01", 2, horizon_tag="tactical", memory_dir=memory_dir
    )
    assert memory.tranche_balances("ACME", memory_dir=memory_dir) == {
        "core": 10.0,
        "tactical": 3.0,
    }


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


def test_compute_drawdown_handles_mixed_naive_and_aware_timestamps(memory_dir):
    """REGRESSION: one naive timestamp in the series used to TypeError against
    the aware ones at sort time — the breaker lost its whole drawdown leg."""
    memory.record_nav_snapshot(
        1_000, timestamp="2026-01-01T00:00:00+00:00", memory_dir=memory_dir
    )
    memory.record_nav_snapshot(
        900, timestamp="2026-01-02T00:00:00", memory_dir=memory_dir  # naive
    )
    dd = memory.compute_drawdown(memory_dir=memory_dir)
    assert dd["samples"] == 2
    assert dd["current_drawdown_pct"] == pytest.approx(10.0)


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
