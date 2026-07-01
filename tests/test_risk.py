"""Behavioral tests for the risk/sizing/autonomy core, including the §B
adversarial scenarios the whole design hinges on."""

from __future__ import annotations

import pytest

from vizier import risk

# ── Conviction sizing ────────────────────────────────────────────────────────


def test_position_size_is_linear_in_conviction(profile):
    # Sizing is linear in conviction, then scaled by the 65% full-size knob.
    full = risk.position_size(slot_base=100, conviction=5, nav=10_000, profile=profile)
    half = risk.position_size(slot_base=100, conviction=2, nav=10_000, profile=profile)
    assert full["size"] == pytest.approx(65.0)  # 100 * 5/5 * 0.65
    assert half["size"] == pytest.approx(26.0)  # 100 * 2/5 * 0.65


def test_conviction_five_sizes_to_fraction_of_cap(profile):
    # With slot_base = the per-asset cap, a max-conviction position targets the
    # configured fraction (65%) of that cap, NOT the full cap — headroom by design.
    nav = 10_000
    cap = nav * profile.max_pct_per_asset / 100  # 2500
    result = risk.position_size(slot_base=cap, conviction=5, nav=nav, profile=profile)
    assert result["size"] == pytest.approx(cap * 0.65)  # 1625
    assert result["capped"] is False


def test_conviction_full_size_knob_is_honored_and_cap_still_hard_caps(profile_path, tmp_path):
    # A profile whose knob is 50 sizes conviction-5 to 50% of the cap...
    yaml_50 = (profile_path.read_text(encoding="utf-8")
               .replace("conviction_full_size_pct_of_cap: 65",
                        "conviction_full_size_pct_of_cap: 50"))
    path = tmp_path / "risk_profile_50.yaml"
    path.write_text(yaml_50, encoding="utf-8")
    profile_50 = risk.load_profile(path)

    nav = 10_000
    cap = nav * profile_50.max_pct_per_asset / 100  # 2500
    at_knob = risk.position_size(slot_base=cap, conviction=5, nav=nav, profile=profile_50)
    assert at_knob["size"] == pytest.approx(cap * 0.50)  # 1250 — the knob is honored

    # ...but the per-asset cap is still the hard ceiling even with a giant slot_base.
    over = risk.position_size(slot_base=1_000_000, conviction=5, nav=nav, profile=profile_50)
    assert over["capped"] is True
    assert over["size"] == pytest.approx(cap)  # never above the per-asset cap


def test_position_size_capped_at_max_pct_per_asset(profile):
    # slot_base far above 25% of a small NAV -> capped at the per-asset limit.
    result = risk.position_size(slot_base=1_000, conviction=5, nav=1_000, profile=profile)
    assert result["capped"] is True
    assert result["size"] == pytest.approx(250.0)  # 25% of 1000


def test_position_size_skips_below_conviction_floor(profile):
    result = risk.position_size(slot_base=100, conviction=1, nav=10_000, profile=profile)
    assert result["skipped"] is True
    assert result["size"] == 0.0


def test_explicit_order_overrides_conviction_floor(profile):
    result = risk.position_size(
        slot_base=100, conviction=1, nav=10_000, profile=profile, explicit_order=True
    )
    assert result["skipped"] is False
    assert result["size"] == pytest.approx(13.0)  # 100 * 1/5 * 0.65


# ── "$100 across N candidates" allocation ────────────────────────────────────


def test_allocate_proportional_to_conviction(profile):
    candidates = [
        {"ticker": "AAA", "conviction": 5},
        {"ticker": "BBB", "conviction": 3},
        {"ticker": "CCC", "conviction": 2},
    ]
    result = risk.allocate_across_candidates(100, candidates, nav=100_000, profile=profile)
    by_ticker = {a["ticker"]: a["size"] for a in result["allocations"]}
    assert by_ticker["AAA"] == pytest.approx(50.0)  # 100 * 5/10
    assert by_ticker["BBB"] == pytest.approx(30.0)  # 100 * 3/10
    assert by_ticker["CCC"] == pytest.approx(20.0)  # 100 * 2/10
    assert result["allocated_total"] == pytest.approx(100.0)


def test_allocate_respects_per_asset_cap(profile):
    # Small NAV: 25% cap = 25 USD. The high-conviction leg's proportional 50 is
    # shaved to the cap; the difference shows up as unallocated, not forced in.
    candidates = [
        {"ticker": "AAA", "conviction": 5},
        {"ticker": "BBB", "conviction": 5},
    ]
    result = risk.allocate_across_candidates(100, candidates, nav=100, profile=profile)
    for leg in result["allocations"]:
        assert leg["size"] <= 25.0 + 1e-9
        assert leg["capped"] is True
        assert leg["over_cap"] is True
    assert result["unallocated"] == pytest.approx(50.0)


def test_allocate_allow_over_cap_honors_full_explicit_amount(profile):
    # The faithful-execution contract: an explicit dollar amount, confirmed once,
    # deploys IN FULL instead of being silently shaved to the per-asset cap. Each
    # over-cap leg is flagged (over_cap) for honest disclosure, but nothing is left
    # unallocated and the leg is NOT marked `capped` (it was not shaved).
    candidates = [
        {"ticker": "AAA", "conviction": 5},
        {"ticker": "BBB", "conviction": 5},
    ]
    result = risk.allocate_across_candidates(
        100, candidates, nav=100, profile=profile, allow_over_cap=True
    )
    for leg in result["allocations"]:
        assert leg["size"] == pytest.approx(50.0)
        assert leg["capped"] is False  # honored in full, not shaved
        assert leg["over_cap"] is True  # but disclosed as over the per-asset cap
    assert result["allocated_total"] == pytest.approx(100.0)
    assert result["unallocated"] == pytest.approx(0.0)
    assert result["allow_over_cap"] is True


def test_allocate_default_still_shaves_over_cap_legs(profile):
    # allow_over_cap defaults False: Vizier-CHOSEN sizing must still respect the cap.
    candidates = [{"ticker": "AAA", "conviction": 5}]
    result = risk.allocate_across_candidates(100, [*candidates], nav=100, profile=profile)
    leg = result["allocations"][0]
    assert leg["size"] == pytest.approx(25.0)
    assert leg["capped"] is True
    assert result["allow_over_cap"] is False


def test_allocate_drops_below_floor_unless_explicit(profile):
    candidates = [
        {"ticker": "AAA", "conviction": 4},
        {"ticker": "LOW", "conviction": 1},
    ]
    dropped = risk.allocate_across_candidates(100, candidates, nav=100_000, profile=profile)
    assert dropped["skipped"] == ["LOW"]
    assert {a["ticker"] for a in dropped["allocations"]} == {"AAA"}

    forced = risk.allocate_across_candidates(
        100, candidates, nav=100_000, profile=profile, explicit_order=True
    )
    assert forced["skipped"] == []
    assert {a["ticker"] for a in forced["allocations"]} == {"AAA", "LOW"}


def test_allocate_per_candidate_explicit_keeps_only_named_subfloor_leg(profile):
    # Mixed request: "$100 into MINE that I named + your best idea, plus a
    # sub-floor pick of yours". The user-named sub-floor leg (explicit) is kept;
    # the skill-derived sub-floor leg is still dropped — without a call-level
    # explicit_order that would floor-exempt everything.
    candidates = [
        {"ticker": "GOOD", "conviction": 4},
        {"ticker": "MINE", "conviction": 1, "explicit": True},
        {"ticker": "WEAK", "conviction": 1},
    ]
    result = risk.allocate_across_candidates(100, candidates, nav=100_000, profile=profile)
    assert result["skipped"] == ["WEAK"]
    assert {a["ticker"] for a in result["allocations"]} == {"GOOD", "MINE"}


def test_allocate_equal_weighting_splits_evenly_not_by_conviction(profile):
    # "split equally across these" must NOT silently conviction-weight.
    candidates = [
        {"ticker": "AAA", "conviction": 5},
        {"ticker": "BBB", "conviction": 3},
        {"ticker": "CCC", "conviction": 2},
    ]
    result = risk.allocate_across_candidates(
        90, candidates, nav=100_000, profile=profile, weighting="equal"
    )
    by_ticker = {a["ticker"]: a["size"] for a in result["allocations"]}
    assert by_ticker["AAA"] == pytest.approx(30.0)
    assert by_ticker["BBB"] == pytest.approx(30.0)
    assert by_ticker["CCC"] == pytest.approx(30.0)
    assert result["weighting"] == "equal"


def test_allocate_explicit_weights_are_honored_and_caps_still_apply(profile):
    # Explicit per-candidate weights override conviction-weighting; the per-asset
    # cap still binds (small NAV: 25% of 200 = 50), shaving the over-weight leg.
    candidates = [
        {"ticker": "AAA", "conviction": 5, "weight": 3},
        {"ticker": "BBB", "conviction": 5, "weight": 1},
    ]
    result = risk.allocate_across_candidates(100, candidates, nav=200, profile=profile)
    by_ticker = {a["ticker"]: a for a in result["allocations"]}
    assert by_ticker["AAA"]["target"] == pytest.approx(75.0)  # 100 * 3/4
    assert by_ticker["AAA"]["size"] == pytest.approx(50.0)  # capped at 25% of 200
    assert by_ticker["AAA"]["capped"] is True
    assert by_ticker["BBB"]["size"] == pytest.approx(25.0)  # 100 * 1/4, under cap
    assert result["weighting"] == "explicit"
    assert result["unallocated"] == pytest.approx(25.0)


def test_allocate_partial_weights_rejected(profile):
    # ANY weight switches the call to explicit-weights mode, where a leg without one
    # would default to 0 and silently receive $0 — the silent-unfaithfulness class.
    # Weights are all-or-none; a partial set must fail loudly.
    with pytest.raises(ValueError, match="every candidate must"):
        risk.allocate_across_candidates(
            100,
            [
                {"ticker": "AAA", "conviction": 4, "weight": 3},
                {"ticker": "BBB", "conviction": 4},
            ],
            nav=100_000,
            profile=profile,
        )


def test_allocate_unknown_weighting_raises(profile):
    with pytest.raises(ValueError, match="unknown weighting"):
        risk.allocate_across_candidates(
            100, [{"ticker": "AAA", "conviction": 4}], nav=100_000,
            profile=profile, weighting="momentum",
        )


def test_allocate_unknown_weighting_raises_even_with_explicit_weights(profile):
    # A garbage weighting must fail loudly EVEN when per-candidate weights are
    # present — it used to be silently ignored on the explicit-weights path.
    with pytest.raises(ValueError, match="unknown weighting"):
        risk.allocate_across_candidates(
            100,
            [
                {"ticker": "AAA", "conviction": 4, "weight": 1},
                {"ticker": "BBB", "conviction": 4, "weight": 2},
            ],
            nav=100_000,
            profile=profile,
            weighting="garbage",
        )


def test_allocate_negative_weight_rejected(profile):
    # A negative weight would otherwise yield a negative (short) size that the
    # upper-bound cap min(target, cap) cannot catch — a buy must never go short.
    with pytest.raises(ValueError, match="weight must be non-negative"):
        risk.allocate_across_candidates(
            100,
            [
                {"ticker": "AAA", "conviction": 4, "weight": -1},
                {"ticker": "BBB", "conviction": 4, "weight": 2},
            ],
            nav=100_000,
            profile=profile,
        )


def test_allocate_negative_conviction_rejected(profile):
    with pytest.raises(ValueError, match="conviction must be non-negative"):
        risk.allocate_across_candidates(
            100,
            [{"ticker": "AAA", "conviction": -3}],
            nav=100_000,
            profile=profile,
        )


def test_allocate_degenerate_weights_fall_back_to_even_split_not_silent_zero(profile):
    # An explicit positive budget is a contract: if the split basis is degenerate
    # (all convictions/weights zero -> weight_sum 0), deploy the full amount evenly
    # instead of silently leaving it all unallocated. Flagged via weight_fallback.
    result = risk.allocate_across_candidates(
        1_000,
        [{"ticker": "AAA", "conviction": 0}, {"ticker": "BBB", "conviction": 0}],
        nav=100_000,
        profile=profile,
        explicit_order=True,
    )
    assert result["weight_fallback"] is True
    assert result["allocated_total"] == pytest.approx(1_000.0)
    assert result["unallocated"] == pytest.approx(0.0)
    by_ticker = {a["ticker"]: a["size"] for a in result["allocations"]}
    assert by_ticker["AAA"] == pytest.approx(500.0)
    assert by_ticker["BBB"] == pytest.approx(500.0)


def test_allocate_rejects_non_finite_amount_and_weight(profile):
    with pytest.raises(ValueError, match="total_amount must be non-negative"):
        risk.allocate_across_candidates(
            float("inf"), [{"ticker": "AAA", "conviction": 3}], nav=100_000, profile=profile
        )
    with pytest.raises(ValueError, match="nav must be positive"):
        risk.allocate_across_candidates(
            100, [{"ticker": "AAA", "conviction": 3}], nav=float("nan"), profile=profile
        )
    with pytest.raises(ValueError, match="weight must be non-negative"):
        risk.allocate_across_candidates(
            100,
            [{"ticker": "AAA", "conviction": 3, "weight": float("nan")}],
            nav=100_000,
            profile=profile,
        )


def test_allocate_zero_weight_is_allowed_and_gets_nothing(profile):
    # Zero weight is valid (not negative): the leg simply receives nothing.
    result = risk.allocate_across_candidates(
        100,
        [
            {"ticker": "AAA", "conviction": 4, "weight": 0},
            {"ticker": "BBB", "conviction": 4, "weight": 1},
        ],
        nav=100_000,
        profile=profile,
    )
    by_ticker = {a["ticker"]: a["size"] for a in result["allocations"]}
    assert by_ticker["AAA"] == pytest.approx(0.0)
    assert by_ticker["BBB"] == pytest.approx(100.0)


# ── Sell-side trim: %/$ -> base quantity (rounds DOWN, never oversells) ───────


def test_trim_quantity_pct_mode():
    result = risk.trim_quantity(current_qty=2.0, pct=30)
    assert result["qty"] == pytest.approx(0.6)
    assert result["mode"] == "pct"


def test_trim_quantity_dollar_mode():
    result = risk.trim_quantity(current_price=2_500.0, dollar_amount=50.0)
    assert result["qty"] == pytest.approx(0.02)  # 50 / 2500
    assert result["mode"] == "dollar"


def test_trim_quantity_floors_to_step_never_up():
    # 0.6 base with a 0.1 step -> exactly 6 steps. 0.65 -> floors to 0.6.
    assert risk.trim_quantity(current_qty=2.0, pct=30, step=0.1)["qty"] == pytest.approx(0.6)
    rounded = risk.trim_quantity(current_qty=1.3, pct=50, step=0.1)  # 0.65 -> 0.6
    assert rounded["qty"] == pytest.approx(0.6)
    assert rounded["rounded_down"] is True
    assert rounded["qty"] <= rounded["raw_qty"] + 1e-9  # never above the raw target


def test_trim_quantity_sub_lot_flags_below_min_lot():
    """A trim smaller than one market lot floors to 0. That is NOT a successful
    trim — the risk reduction silently doesn't happen — so it must be flagged so
    the skill can surface it instead of sending a zero-qty exit."""
    result = risk.trim_quantity(current_qty=100.0, pct=0.5, step=1.0)  # 0.5 share -> floors to 0
    assert result["qty"] == 0.0
    assert result["below_min_lot"] is True
    assert result["rounded_down"] is True


def test_trim_quantity_normal_trim_is_not_below_min_lot():
    result = risk.trim_quantity(current_qty=2.0, pct=30, step=0.1)
    assert result["qty"] == pytest.approx(0.6)
    assert result["below_min_lot"] is False


def test_trim_quantity_caps_at_holding():
    # A dollar trim larger than the position must not oversell.
    result = risk.trim_quantity(current_qty=1.0, current_price=100.0, dollar_amount=500.0)
    assert result["qty"] == pytest.approx(1.0)
    assert result["capped_to_holding"] is True


def test_trim_quantity_validates_inputs():
    with pytest.raises(ValueError, match="current_qty"):
        risk.trim_quantity(pct=30)  # pct without current_qty
    with pytest.raises(ValueError, match="current_price"):
        risk.trim_quantity(dollar_amount=50)  # dollar without price
    with pytest.raises(ValueError, match="pct OR dollar_amount"):
        risk.trim_quantity(current_qty=2, pct=30, dollar_amount=50)
    with pytest.raises(ValueError, match="pct must be"):
        risk.trim_quantity(current_qty=2, pct=150)


# ── Position limits — each violation type ────────────────────────────────────


def _portfolio(nav=10_000, cash=5_000, positions=None):
    return {"nav": nav, "cash": cash, "positions": positions or []}


def test_limits_allows_within_bounds(profile):
    result = risk.check_position_limits(
        _portfolio(), {"ticker": "AAA", "value": 1_000, "sector": "Tech"}, profile
    )
    assert result["allowed"] is True
    assert result["violations"] == []


def test_limits_flags_max_pct_per_asset(profile):
    result = risk.check_position_limits(
        _portfolio(), {"ticker": "AAA", "value": 3_000, "sector": "Tech"}, profile
    )
    assert any(v["type"] == "max_pct_per_asset" for v in result["violations"])


def test_limits_flags_max_pct_per_sector(profile):
    positions = [{"ticker": "AAA", "value": 3_500, "sector": "Tech"}]
    result = risk.check_position_limits(
        _portfolio(positions=positions),
        {"ticker": "BBB", "value": 1_000, "sector": "Tech"},
        profile,
    )
    assert any(v["type"] == "max_pct_per_sector" for v in result["violations"])


def test_limits_flags_min_cash(profile):
    result = risk.check_position_limits(
        _portfolio(cash=600), {"ticker": "AAA", "value": 400, "sector": "Tech"}, profile
    )
    # cash would fall to 200/10000 = 2% < 5% floor.
    assert any(v["type"] == "min_cash_pct" for v in result["violations"])


def test_limits_flags_max_positions(profile):
    positions = [
        {"ticker": f"T{i}", "value": 100, "sector": "Tech"} for i in range(8)
    ]
    result = risk.check_position_limits(
        _portfolio(positions=positions),
        {"ticker": "NEW", "value": 100, "sector": "Tech"},
        profile,
    )
    assert any(v["type"] == "max_positions" for v in result["violations"])


def test_limits_exactly_on_cap_is_allowed(profile):
    # 2500 is exactly 25% of 10000 — on the cap, not over it.
    result = risk.check_position_limits(
        _portfolio(), {"ticker": "AAA", "value": 2_500, "sector": "Tech"}, profile
    )
    assert all(v["type"] != "max_pct_per_asset" for v in result["violations"])


# ── Circuit breaker ──────────────────────────────────────────────────────────


def test_breaker_trips_on_vix(profile):
    status = risk.circuit_breaker_status(vix=40, monthly_drawdown_pct=5, profile=profile)
    assert status["tripped"] is True
    assert [leg["leg"] for leg in status["legs"]] == ["vix"]


def test_breaker_trips_on_drawdown(profile):
    status = risk.circuit_breaker_status(vix=15, monthly_drawdown_pct=20, profile=profile)
    assert status["tripped"] is True
    assert [leg["leg"] for leg in status["legs"]] == ["monthly_drawdown"]


def test_breaker_quiet_within_limits(profile):
    status = risk.circuit_breaker_status(vix=15, monthly_drawdown_pct=5, profile=profile)
    assert status["tripped"] is False


def test_breaker_ignores_missing_legs(profile):
    status = risk.circuit_breaker_status(vix=None, monthly_drawdown_pct=None, profile=profile)
    assert status["tripped"] is False


# ── §B: the cumulative-ceiling drain attack ──────────────────────────────────


def test_cumulative_ceiling_blocks_loop_drain(profile):
    """A /loop where every round sits inside the per-run cap but the cumulative
    24h ceiling must block once 50% of the FIXED baseline NAV is spent.

    Per-run cap alone would NOT stop this: each round mobilizes 33% of what is
    left, so the account would drain while every round looks compliant.
    """
    baseline = 1_000.0
    per_run_cap = baseline * profile.autonomy.per_run_pct / 100  # 330

    approved: list[float] = []
    blocked = False

    # Each round tries to spend 200 — comfortably inside the 330 per-run cap.
    # Running spend/trade-count come from what was approved so far (mirrors the
    # decision log), with the baseline held FIXED across the whole loop.
    for _ in range(10):
        candidate = 200.0
        # Sanity: each round IS within the per-run cap, proving the cap is not
        # what stops the drain.
        assert candidate <= per_run_cap
        decision = risk.cumulative_ceiling_check(
            baseline_nav_start_of_day=baseline,
            spent_today=sum(approved),
            trades_today=len(approved),
            candidate_value=candidate,
            profile=profile,
        )
        if not decision["allowed"]:
            blocked = True
            break
        approved.append(candidate)

    # It must have blocked, and never let cumulative spend exceed 50% of baseline.
    assert blocked is True
    assert sum(approved) <= baseline * 0.50 + 1e-9
    assert sum(approved) <= 500.0


def test_cumulative_ceiling_anchored_to_fixed_baseline(profile):
    """A shrinking account must not re-authorize fresh slices: the budget is a
    function of the FIXED start-of-day baseline, not the current (lower) NAV."""
    baseline = 1_000.0
    # 480 already spent today; remaining budget is fixed at 500 - 480 = 20,
    # independent of how far the live account has fallen.
    decision = risk.cumulative_ceiling_check(
        baseline_nav_start_of_day=baseline,
        spent_today=480,
        trades_today=3,
        candidate_value=25,
        profile=profile,
    )
    assert decision["allowed"] is False
    assert decision["remaining_budget"] == pytest.approx(20.0)


def test_cumulative_ceiling_trade_count_backstop(profile):
    """Even with budget to spare, the daily trade-count ceiling must bite."""
    decision = risk.cumulative_ceiling_check(
        baseline_nav_start_of_day=1_000_000,  # huge budget, never the binding leg
        spent_today=0,
        trades_today=profile.autonomy.daily_max_trades,  # already at the cap
        candidate_value=1,
        profile=profile,
    )
    assert decision["allowed"] is False
    assert "trade-count" in decision["reason"]


def test_cumulative_ceiling_allows_within_budget(profile):
    decision = risk.cumulative_ceiling_check(
        baseline_nav_start_of_day=1_000,
        spent_today=100,
        trades_today=1,
        candidate_value=200,
        profile=profile,
    )
    assert decision["allowed"] is True
    assert decision["remaining_budget"] == pytest.approx(400.0)  # 500 - 100


# ── §F: per-run ceiling (33% of NAV OR per_run_max_trades) ───────────────────


def test_per_run_ceiling_value_leg(profile):
    # 33% of 1000 = 330 budget; 200 already spent this run.
    decision = risk.per_run_ceiling_check(
        baseline_nav_start_of_day=1_000,
        spent_this_run=200,
        trades_this_run=1,
        candidate_value=200,  # 200 + 200 = 400 > 330
        profile=profile,
    )
    assert decision["allowed"] is False
    assert "per-run value ceiling" in decision["reason"]
    assert decision["remaining_budget"] == pytest.approx(130.0)  # 330 - 200


def test_per_run_ceiling_count_leg(profile):
    decision = risk.per_run_ceiling_check(
        baseline_nav_start_of_day=1_000_000,  # value never the binding leg
        spent_this_run=0,
        trades_this_run=profile.autonomy.per_run_max_trades,  # already at the per-run cap
        candidate_value=1,
        profile=profile,
    )
    assert decision["allowed"] is False
    assert "trade-count" in decision["reason"]


def test_per_run_ceiling_uses_fixed_baseline_not_current_nav(profile):
    # Anchored to the passed (fixed) baseline; 100 of a 330 budget leaves 230.
    decision = risk.per_run_ceiling_check(
        baseline_nav_start_of_day=1_000,
        spent_this_run=100,
        trades_this_run=1,
        candidate_value=150,
        profile=profile,
    )
    assert decision["allowed"] is True
    assert decision["budget"] == pytest.approx(330.0)
    assert decision["remaining_budget"] == pytest.approx(230.0)


# ── §B: drawdown kill ────────────────────────────────────────────────────────


def test_drawdown_kill_fires_at_threshold():
    result = risk.drawdown_kill_check(
        nav_at_loop_start=1_000, current_nav=850, drawdown_kill_pct=15
    )
    assert result["kill"] is True
    assert result["drawdown_pct"] == pytest.approx(15.0)
    assert result["action"] == "disarm_autonomy_require_manual_rearm"


def test_drawdown_kill_quiet_below_threshold():
    result = risk.drawdown_kill_check(
        nav_at_loop_start=1_000, current_nav=900, drawdown_kill_pct=15
    )
    assert result["kill"] is False
    assert result["drawdown_pct"] == pytest.approx(10.0)


def test_drawdown_kill_gains_are_zero_drawdown():
    result = risk.drawdown_kill_check(
        nav_at_loop_start=1_000, current_nav=1_200, drawdown_kill_pct=15
    )
    assert result["kill"] is False
    assert result["drawdown_pct"] == 0.0


# ── Profile loading ──────────────────────────────────────────────────────────


def test_load_profile_resolves_active(profile_path):
    loaded = risk.load_profile(profile_path)  # no name -> active_profile
    assert loaded.name == "aggressive"
    assert loaded.max_pct_per_asset == 25


def test_load_profile_unknown_name_raises(profile_path):
    with pytest.raises(ValueError, match="not found"):
        risk.load_profile(profile_path, "does-not-exist")
