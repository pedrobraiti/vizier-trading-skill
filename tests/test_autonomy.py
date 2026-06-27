"""Behavioral tests for the §B seam: the persisted day baseline, code-sourced
running totals, and the composed gate run through the real stateful path
(write executed decisions to disk, then gate against them) — never an in-test
sum. All deterministic: injected ``now``, temp ``memory_dir``, no real git."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vizier import autonomy, memory

NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)


def _buy(ticker, value, *, at):
    return {
        "timestamp": at.isoformat(),
        "intent": f"buy {ticker}",
        "mode": "autonomy",
        "executed_orders": [{"side": "BUY", "ticker": ticker, "value": value, "venue": "ibkr"}],
    }


def _sell(ticker, value, *, at):
    return {
        "timestamp": at.isoformat(),
        "intent": f"sell {ticker}",
        "mode": "autonomy",
        "executed_orders": [{"side": "SELL", "ticker": ticker, "value": value, "venue": "ibkr"}],
    }


# ── Day-state aggregation from the decision log ──────────────────────────────


def test_day_state_counts_buys_not_sells_and_drops_pre_window(memory_dir):
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)

    # A buy BEFORE the window must not count toward the day.
    memory.append_decision(_buy("OLD", 300, at=NOW - timedelta(hours=1)), memory_dir=memory_dir)
    # In-window: buy + sell + buy, plus a research-only (no executed_orders).
    memory.append_decision(_buy("AAA", 200, at=NOW + timedelta(minutes=1)), memory_dir=memory_dir)
    memory.append_decision(_sell("AAA", 95, at=NOW + timedelta(minutes=2)), memory_dir=memory_dir)
    memory.append_decision(_buy("BBB", 100, at=NOW + timedelta(minutes=3)), memory_dir=memory_dir)
    memory.append_decision(
        {"timestamp": (NOW + timedelta(minutes=4)).isoformat(), "intent": "no edge",
         "executed_orders": []},
        memory_dir=memory_dir,
    )

    state = memory.autonomy_day_state(now=NOW + timedelta(minutes=5), memory_dir=memory_dir)
    assert state["armed"] is True
    assert state["spent_today"] == pytest.approx(300.0)  # 200 + 100; sell & pre-window excluded
    assert state["trades_today"] == 3  # buy, sell, buy; research-only contributes nothing
    assert len(state["executed_orders"]) == 3


def test_day_state_not_armed_is_blocked_shaped(memory_dir):
    state = memory.autonomy_day_state(now=NOW, memory_dir=memory_dir)
    assert state["armed"] is False
    assert state["spent_today"] == 0.0
    assert state["trades_today"] == 0


def test_day_state_expires_after_window(memory_dir):
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    # 25h later the window has expired -> not armed until re-arm.
    state = memory.autonomy_day_state(now=NOW + timedelta(hours=25), memory_dir=memory_dir)
    assert state["armed"] is False


def test_baseline_is_fixed_at_arm_time(memory_dir):
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    base = memory.current_day_baseline(now=NOW + timedelta(hours=1), memory_dir=memory_dir)
    assert base is not None
    assert base["baseline_nav"] == 1_000


def test_rearm_refused_mid_window_and_state_unchanged(memory_dir):
    """The §B drain guard: re-arming while the window is still active is REFUSED
    in code (it would reset window_start and wipe the day's spend). The original
    baseline/window must be untouched after the refusal."""
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    # Spend 200 today so we can prove the spend is NOT wiped by the refusal.
    memory.append_decision(_buy("AAA", 200, at=NOW + timedelta(minutes=1)), memory_dir=memory_dir)

    with pytest.raises(memory.AutonomyAlreadyArmedError, match="already armed"):
        memory.arm_autonomy(800, now=NOW + timedelta(hours=2), memory_dir=memory_dir)

    base = memory.current_day_baseline(now=NOW + timedelta(hours=3), memory_dir=memory_dir)
    assert base["baseline_nav"] == 1_000  # original baseline survives the refusal
    state = memory.autonomy_day_state(now=NOW + timedelta(hours=3), memory_dir=memory_dir)
    assert state["window_start"] == NOW.isoformat()  # window NOT reset
    assert state["spent_today"] == pytest.approx(200.0)  # spend NOT wiped -> no drain


def test_rearm_allowed_after_disarm(memory_dir):
    """The legitimate path: an explicit disarm (e.g. post-kill) clears the active
    window, so a re-arm at a fresh baseline is allowed."""
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    memory.disarm_autonomy(now=NOW + timedelta(hours=2), memory_dir=memory_dir)
    rearm_at = NOW + timedelta(hours=2, minutes=1)
    active = memory.arm_autonomy(800, now=rearm_at, memory_dir=memory_dir)
    assert active["baseline_nav"] == 800
    base = memory.current_day_baseline(now=NOW + timedelta(hours=3), memory_dir=memory_dir)
    assert base["baseline_nav"] == 800


def test_rearm_allowed_after_window_expiry(memory_dir):
    """Once the 24h window has expired there is nothing to drain, so a fresh arm
    is allowed and supersedes with the new baseline."""
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    active = memory.arm_autonomy(800, now=NOW + timedelta(hours=25), memory_dir=memory_dir)
    assert active["baseline_nav"] == 800
    later = NOW + timedelta(hours=25, minutes=1)
    base = memory.current_day_baseline(now=later, memory_dir=memory_dir)
    assert base["baseline_nav"] == 800


def test_disarm_blocks_until_rearm(memory_dir):
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    memory.disarm_autonomy(now=NOW + timedelta(minutes=1), memory_dir=memory_dir)
    base = memory.current_day_baseline(now=NOW + timedelta(minutes=2), memory_dir=memory_dir)
    assert base is None


# ── The composed gate (per-run · daily · kill · armed) ───────────────────────


def test_gate_blocks_when_not_armed(profile, memory_dir):
    gate = autonomy.check_autonomy_gate(
        candidate_value=10, current_nav=1_000, profile=profile, now=NOW, memory_dir=memory_dir
    )
    assert gate["allowed"] is False
    assert [b["type"] for b in gate["blocks"]] == ["not_armed"]


def test_gate_blocks_without_begin_run(profile, memory_dir):
    """Armed but no run started -> the gate refuses: without a run marker the
    per-run cap cannot be enforced, so there is no per-run safety to lean on."""
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    gate = autonomy.check_autonomy_gate(
        candidate_value=10,
        current_nav=1_000,
        profile=profile,
        now=NOW + timedelta(minutes=1),
        memory_dir=memory_dir,
    )
    assert gate["allowed"] is False
    assert gate["run_active"] is False
    assert any(b["type"] == "per_run_ceiling" for b in gate["blocks"])


def test_per_run_value_ceiling_blocks_with_daily_budget_to_spare(profile, memory_dir):
    """Within ONE run, the per-run cap (33% of the fixed baseline = 330) binds
    BEFORE the daily ceiling (500) -- proving the per-run leg is real."""
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    memory.begin_run(now=NOW + timedelta(minutes=1), memory_dir=memory_dir)
    memory.append_decision(_buy("AAA", 200, at=NOW + timedelta(minutes=2)), memory_dir=memory_dir)

    gate = autonomy.check_autonomy_gate(
        candidate_value=200,  # run total -> 400 > 330 per-run, but 400 <= 500 daily
        current_nav=1_000,
        profile=profile,
        now=NOW + timedelta(minutes=3),
        memory_dir=memory_dir,
    )
    assert gate["allowed"] is False
    block_types = [b["type"] for b in gate["blocks"]]
    assert "per_run_ceiling" in block_types
    assert "cumulative_ceiling" not in block_types  # daily still had room
    assert gate["spent_this_run"] == pytest.approx(200.0)


def test_per_run_trade_count_blocks_within_run(profile, memory_dir):
    """per_run_max_trades (5 in the test profile) bites even on tiny values."""
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    memory.begin_run(now=NOW + timedelta(minutes=1), memory_dir=memory_dir)
    for i in range(5):  # 5 trades already this run
        memory.append_decision(
            _buy(f"T{i}", 1, at=NOW + timedelta(minutes=2 + i)), memory_dir=memory_dir
        )

    gate = autonomy.check_autonomy_gate(
        candidate_value=1,  # trivially within both value budgets
        current_nav=1_000,
        profile=profile,
        now=NOW + timedelta(minutes=10),
        memory_dir=memory_dir,
    )
    assert gate["allowed"] is False
    per_run_block = next(b for b in gate["blocks"] if b["type"] == "per_run_ceiling")
    assert "trade-count" in per_run_block["message"]


def test_per_run_resets_each_run_while_daily_accumulates(profile, memory_dir):
    """Both anchors coexist: a fresh begin_run resets the per-run counter, but
    the DAILY total keeps accumulating from the arm window until it blocks at
    50% of the fixed baseline. Everything runs through the real stateful gate."""
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)

    # Round A: begin a run, deploy 200 + 100 = 300 (within the 330 per-run cap).
    memory.begin_run(now=NOW + timedelta(minutes=1), memory_dir=memory_dir)
    for value, minute in ((200, 2), (100, 3)):
        g = autonomy.check_autonomy_gate(
            candidate_value=value, current_nav=1_000, profile=profile,
            now=NOW + timedelta(minutes=minute), memory_dir=memory_dir,
        )
        assert g["allowed"] is True
        memory.append_decision(
            _buy("A", value, at=NOW + timedelta(minutes=minute)), memory_dir=memory_dir
        )

    # Round B: a fresh begin_run RESETS the per-run counter. A 200 here would be
    # 300+200=500 within the run if it hadn't reset (> 330) -> the fact it's
    # allowed proves the reset. Day total is now 500 (= 300 + 200).
    memory.begin_run(now=NOW + timedelta(minutes=10), memory_dir=memory_dir)
    g = autonomy.check_autonomy_gate(
        candidate_value=200, current_nav=1_000, profile=profile,
        now=NOW + timedelta(minutes=11), memory_dir=memory_dir,
    )
    assert g["allowed"] is True  # per-run reset let it through
    assert g["spent_this_run"] == pytest.approx(0.0)
    memory.append_decision(_buy("B", 200, at=NOW + timedelta(minutes=11)), memory_dir=memory_dir)

    # Next candidate: per-run still has room (200 of 330), but the DAILY ceiling
    # is full (500/500) -> blocks on cumulative_ceiling, not per_run.
    g = autonomy.check_autonomy_gate(
        candidate_value=50, current_nav=1_000, profile=profile,
        now=NOW + timedelta(minutes=12), memory_dir=memory_dir,
    )
    assert g["allowed"] is False
    block_types = [b["type"] for b in g["blocks"]]
    assert "cumulative_ceiling" in block_types
    assert "per_run_ceiling" not in block_types

    day = memory.autonomy_day_state(now=NOW + timedelta(minutes=12), memory_dir=memory_dir)
    assert day["spent_today"] == pytest.approx(500.0)


def test_gate_drawdown_kill_fires_even_with_budget(profile, memory_dir):
    """Both value budgets wide open, but NAV has fallen past the kill threshold
    -> the gate still blocks on the drawdown kill."""
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    memory.begin_run(now=NOW + timedelta(minutes=1), memory_dir=memory_dir)
    gate = autonomy.check_autonomy_gate(
        candidate_value=10,  # trivially within budget
        current_nav=850,  # 15% down == kill threshold for the test profile
        profile=profile,
        now=NOW + timedelta(minutes=2),
        memory_dir=memory_dir,
    )
    assert gate["allowed"] is False
    assert gate["kill"] is True
    assert any(b["type"] == "drawdown_kill" for b in gate["blocks"])


def test_drawdown_kill_latches_across_nav_recovery(profile, memory_dir):
    """Once the kill fires, autonomy stays killed even if NAV recovers above the
    threshold — the latch is persisted, so the skill failing to disarm cannot
    silently re-enable autonomy on a bounce."""
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    memory.begin_run(now=NOW + timedelta(minutes=1), memory_dir=memory_dir)

    # NAV dips to the kill threshold -> gate blocks AND latches.
    killed = autonomy.check_autonomy_gate(
        candidate_value=10, current_nav=850, profile=profile,
        now=NOW + timedelta(minutes=2), memory_dir=memory_dir,
    )
    assert killed["allowed"] is False
    assert any(b["type"] == "drawdown_kill" for b in killed["blocks"])
    assert memory.drawdown_kill_latch(memory_dir=memory_dir)["killed"] is True

    # NAV recovers fully, but the latch keeps autonomy killed.
    recovered = autonomy.check_autonomy_gate(
        candidate_value=10, current_nav=1_000, profile=profile,
        now=NOW + timedelta(minutes=3), memory_dir=memory_dir,
    )
    assert recovered["allowed"] is False
    assert recovered["kill"] is True
    assert recovered["kill_latched"] is True
    assert any(b["type"] == "drawdown_kill" for b in recovered["blocks"])


def test_drawdown_kill_latch_clears_on_disarm_and_fresh_arm(profile, memory_dir):
    """The latch clears only on the explicit manual path: disarm, then a fresh
    arm re-opens a clean window and the gate allows again."""
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    memory.begin_run(now=NOW + timedelta(minutes=1), memory_dir=memory_dir)
    autonomy.check_autonomy_gate(
        candidate_value=10, current_nav=850, profile=profile,
        now=NOW + timedelta(minutes=2), memory_dir=memory_dir,
    )
    assert memory.drawdown_kill_latch(memory_dir=memory_dir)["killed"] is True

    # A killed-but-still-in-window user must disarm first, then re-arm.
    memory.disarm_autonomy(now=NOW + timedelta(minutes=3), memory_dir=memory_dir)
    memory.arm_autonomy(1_000, now=NOW + timedelta(minutes=4), memory_dir=memory_dir)
    memory.begin_run(now=NOW + timedelta(minutes=5), memory_dir=memory_dir)
    assert memory.drawdown_kill_latch(memory_dir=memory_dir)["killed"] is False

    gate = autonomy.check_autonomy_gate(
        candidate_value=10, current_nav=990, profile=profile,
        now=NOW + timedelta(minutes=6), memory_dir=memory_dir,
    )
    assert gate["allowed"] is True
    assert gate["kill_latched"] is False


def test_gate_allows_within_all_limits(profile, memory_dir):
    memory.arm_autonomy(1_000, now=NOW, memory_dir=memory_dir)
    memory.begin_run(now=NOW + timedelta(minutes=1), memory_dir=memory_dir)
    gate = autonomy.check_autonomy_gate(
        candidate_value=100,
        current_nav=990,
        profile=profile,
        now=NOW + timedelta(minutes=2),
        memory_dir=memory_dir,
    )
    assert gate["allowed"] is True
    assert gate["blocks"] == []
    assert gate["run_active"] is True
    assert gate["remaining_budget"] == pytest.approx(500.0)  # daily: 50% of 1000, nothing spent
    assert gate["per_run_remaining_budget"] == pytest.approx(330.0)  # per-run: 33% of 1000
