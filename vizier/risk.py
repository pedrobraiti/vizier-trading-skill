"""Deterministic risk, sizing and autonomy math.

Pure functions: no I/O, no network, no clock. Everything the function needs is
passed in; everything it decides is returned as a plain JSON-serializable dict
so the skill can act on it and the CLI can wrap it in an `{ok, data}` envelope.

These are the money-sensitive guarantees that must NOT live in the LLM's free
judgment (it can forget between rounds). The §B fixes — the cumulative daily
ceiling and the drawdown kill — live here, exact and stateless.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .constants import LIMIT_EPSILON, MAX_CONVICTION, PERCENT

# ── Profile model ────────────────────────────────────────────────────────────
# A frozen, typed view over `config/risk_profile.yaml`. The YAML stays the
# editable surface (Pedro tweaks numbers there); this is just structured access.


@dataclass(frozen=True)
class CircuitBreaker:
    vix_threshold: float
    monthly_drawdown_pct: float


@dataclass(frozen=True)
class Autonomy:
    per_run_pct: float
    per_run_max_trades: int
    daily_cumulative_pct: float
    daily_max_trades: int
    drawdown_kill_pct: float


@dataclass(frozen=True)
class RiskProfile:
    name: str
    max_pct_per_asset: float
    max_pct_per_sector: float
    min_cash_pct: float
    max_positions: int
    conviction_floor: int
    circuit_breaker: CircuitBreaker
    autonomy: Autonomy

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_profile(path: str | Path, name: str | None = None) -> RiskProfile:
    """Load a risk profile from YAML, resolving ``active_profile`` when no name.

    Raises ValueError with a clear message if the file or the named profile is
    malformed, so a config typo fails loudly instead of silently mis-sizing.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"risk profile file is not a mapping: {path}")

    resolved_name = name or raw.get("active_profile")
    if not resolved_name:
        raise ValueError("no profile name given and `active_profile` is unset")

    profiles = raw.get("profiles") or {}
    if resolved_name not in profiles:
        available = ", ".join(sorted(profiles)) or "(none)"
        raise ValueError(f"profile '{resolved_name}' not found; available: {available}")

    body = profiles[resolved_name]
    breaker = body["circuit_breaker"]
    autonomy = body["autonomy"]
    return RiskProfile(
        name=resolved_name,
        max_pct_per_asset=float(body["max_pct_per_asset"]),
        max_pct_per_sector=float(body["max_pct_per_sector"]),
        min_cash_pct=float(body["min_cash_pct"]),
        max_positions=int(body["max_positions"]),
        conviction_floor=int(raw.get("conviction_floor", 0)),
        circuit_breaker=CircuitBreaker(
            vix_threshold=float(breaker["vix_threshold"]),
            monthly_drawdown_pct=float(breaker["monthly_drawdown_pct"]),
        ),
        autonomy=Autonomy(
            per_run_pct=float(autonomy["per_run_pct"]),
            per_run_max_trades=int(autonomy["per_run_max_trades"]),
            daily_cumulative_pct=float(autonomy["daily_cumulative_pct"]),
            daily_max_trades=int(autonomy["daily_max_trades"]),
            drawdown_kill_pct=float(autonomy["drawdown_kill_pct"]),
        ),
    )


# ── Sizing ───────────────────────────────────────────────────────────────────


def _asset_cap(nav: float, profile: RiskProfile) -> float:
    """Max USD allowed in a single asset = max-%/asset of NAV."""
    return nav * profile.max_pct_per_asset / PERCENT


def position_size(
    slot_base: float,
    conviction: int,
    nav: float,
    profile: RiskProfile,
    *,
    explicit_order: bool = False,
) -> dict[str, Any]:
    """Linear conviction sizing: ``slot_base * (conviction/5)`` capped at the
    per-asset NAV limit.

    Conviction below the profile floor is skipped (size 0) unless ``explicit_order``
    is set for this exact ticker — honesty over a forced trade, but an explicit
    order is still honored (spec, §10 + §E).
    """
    if nav <= 0:
        raise ValueError("nav must be positive")

    skipped = conviction < profile.conviction_floor and not explicit_order
    if skipped:
        return {
            "size": 0.0,
            "capped": False,
            "skipped": True,
            "reason": (
                f"conviction {conviction} below floor {profile.conviction_floor} "
                "and no explicit order"
            ),
        }

    raw = slot_base * (conviction / MAX_CONVICTION)
    cap = _asset_cap(nav, profile)
    size = min(raw, cap)
    capped = raw > cap + LIMIT_EPSILON
    return {
        "size": size,
        "raw_size": raw,
        "cap": cap,
        "capped": capped,
        "skipped": False,
        "reason": (
            f"capped at {profile.max_pct_per_asset:g}% of NAV" if capped else "linear by conviction"
        ),
    }


def allocate_across_candidates(
    total_amount: float,
    candidates: list[dict[str, Any]],
    nav: float,
    profile: RiskProfile,
    *,
    explicit_order: bool = False,
) -> dict[str, Any]:
    """Split a fixed budget across candidates weighted by conviction.

    Allocation_i = ``total * conviction_i / Σconviction`` (spec, §F "$100 em 3"),
    then each leg is capped at the per-asset NAV limit. Candidates below the
    conviction floor are dropped unless ``explicit_order`` is set. Whatever the
    caps shave off is reported as ``unallocated`` rather than silently forced
    back in (forcing it could re-breach a cap).

    Each candidate dict needs at least ``ticker`` and ``conviction``.
    """
    if nav <= 0:
        raise ValueError("nav must be positive")
    if total_amount < 0:
        raise ValueError("total_amount must be non-negative")

    eligible = [
        c
        for c in candidates
        if explicit_order or c.get("conviction", 0) >= profile.conviction_floor
    ]
    skipped = [
        c["ticker"]
        for c in candidates
        if not (explicit_order or c.get("conviction", 0) >= profile.conviction_floor)
    ]

    conviction_sum = sum(c.get("conviction", 0) for c in eligible)
    cap = _asset_cap(nav, profile)

    allocations: list[dict[str, Any]] = []
    allocated_total = 0.0
    for c in eligible:
        weight = (c.get("conviction", 0) / conviction_sum) if conviction_sum > 0 else 0.0
        target = total_amount * weight
        size = min(target, cap)
        allocated_total += size
        allocations.append(
            {
                "ticker": c["ticker"],
                "conviction": c.get("conviction", 0),
                "target": target,
                "size": size,
                "capped": target > cap + LIMIT_EPSILON,
            }
        )

    return {
        "allocations": allocations,
        "skipped": skipped,
        "allocated_total": allocated_total,
        "unallocated": max(0.0, total_amount - allocated_total),
        "asset_cap": cap,
    }


# ── Sell-side sizing: %/$ -> base quantity ───────────────────────────────────


def trim_quantity(
    *,
    current_qty: float | None = None,
    pct: float | None = None,
    current_price: float | None = None,
    dollar_amount: float | None = None,
    step: float | None = None,
) -> dict[str, Any]:
    """Convert a ``%`` or ``$`` trim into a base sell quantity, rounding **DOWN**.

    Sells are by quantity (both venues), so "trim 30% of ETH" / "sell $50 of SOL"
    is a money-adjacent conversion the LLM should not do by hand (spec, Rule #2).
    Two modes:
      * **pct**    — ``current_qty * pct/100`` (needs ``current_qty``).
      * **dollar** — ``dollar_amount / current_price`` (needs ``current_price``).
    The result is capped at ``current_qty`` when known (never oversell) and floored
    to ``step`` (the market lot size / amount precision) when given — always DOWN,
    so a rounding error can only sell slightly less, never more than intended.
    """
    if pct is not None and dollar_amount is not None:
        raise ValueError("pass pct OR dollar_amount, not both")

    if pct is not None:
        if current_qty is None:
            raise ValueError("pct trim needs current_qty")
        if not 0 < pct <= PERCENT:
            raise ValueError("pct must be in (0, 100]")
        raw = current_qty * pct / PERCENT
        mode = "pct"
    elif dollar_amount is not None:
        if current_price is None or current_price <= 0:
            raise ValueError("dollar trim needs a positive current_price")
        if dollar_amount < 0:
            raise ValueError("dollar_amount must be non-negative")
        raw = dollar_amount / current_price
        mode = "dollar"
    else:
        raise ValueError("provide pct (with current_qty) or dollar_amount (with current_price)")

    capped_to_holding = False
    if current_qty is not None and raw > current_qty:
        raw = current_qty
        capped_to_holding = True

    qty = raw
    if step is not None:
        if step <= 0:
            raise ValueError("step must be positive")
        # Floor to a whole number of steps. Round the ratio to kill float dust
        # BEFORE flooring so e.g. 2.9999999999 steps doesn't drop a whole step,
        # while never rounding UP past `raw` (the no-oversell guarantee).
        units = math.floor(round(raw / step, 9))
        qty = round(units * step, 12)

    return {
        "qty": qty,
        "raw_qty": raw,
        "mode": mode,
        "step": step,
        "rounded_down": qty < raw - LIMIT_EPSILON,
        "capped_to_holding": capped_to_holding,
    }


# ── Portfolio limits ─────────────────────────────────────────────────────────


def check_position_limits(
    portfolio: dict[str, Any],
    candidate: dict[str, Any],
    profile: RiskProfile,
) -> dict[str, Any]:
    """Project a candidate buy onto the portfolio and flag every limit breach.

    ``portfolio`` = ``{"nav", "cash", "positions": [{"ticker","value","sector"}]}``.
    ``candidate`` = ``{"ticker","value","sector"}`` (a buy; value in USD).

    Denominator is NAV for every percentage limit (spec, §E). A position sitting
    exactly on a cap is allowed; only strictly over is a violation.
    """
    nav = float(portfolio["nav"])
    cash = float(portfolio.get("cash", 0.0))
    positions = portfolio.get("positions", [])
    if nav <= 0:
        raise ValueError("nav must be positive")

    ticker = candidate["ticker"]
    sector = candidate.get("sector")
    value = float(candidate["value"])

    held_tickers = {p["ticker"] for p in positions}
    is_new_position = ticker not in held_tickers

    existing_ticker_value = sum(float(p["value"]) for p in positions if p["ticker"] == ticker)
    existing_sector_value = sum(
        float(p["value"]) for p in positions if sector is not None and p.get("sector") == sector
    )

    post_ticker_pct = (existing_ticker_value + value) / nav * PERCENT
    post_sector_pct = (existing_sector_value + value) / nav * PERCENT
    post_cash_pct = (cash - value) / nav * PERCENT
    post_position_count = len(held_tickers) + (1 if is_new_position else 0)

    violations: list[dict[str, Any]] = []

    if post_ticker_pct > profile.max_pct_per_asset + LIMIT_EPSILON:
        violations.append(
            {
                "type": "max_pct_per_asset",
                "limit_pct": profile.max_pct_per_asset,
                "projected_pct": post_ticker_pct,
                "message": (
                    f"{ticker} would be {post_ticker_pct:.1f}% of NAV "
                    f"(limit {profile.max_pct_per_asset:g}%)"
                ),
            }
        )

    if sector is not None and post_sector_pct > profile.max_pct_per_sector + LIMIT_EPSILON:
        violations.append(
            {
                "type": "max_pct_per_sector",
                "limit_pct": profile.max_pct_per_sector,
                "projected_pct": post_sector_pct,
                "message": (
                    f"sector {sector} would be {post_sector_pct:.1f}% of NAV "
                    f"(limit {profile.max_pct_per_sector:g}%)"
                ),
            }
        )

    if post_cash_pct < profile.min_cash_pct - LIMIT_EPSILON:
        violations.append(
            {
                "type": "min_cash_pct",
                "limit_pct": profile.min_cash_pct,
                "projected_pct": post_cash_pct,
                "message": (
                    f"cash would fall to {post_cash_pct:.1f}% of NAV "
                    f"(floor {profile.min_cash_pct:g}%)"
                ),
            }
        )

    if is_new_position and post_position_count > profile.max_positions:
        violations.append(
            {
                "type": "max_positions",
                "limit": profile.max_positions,
                "projected": post_position_count,
                "message": (
                    f"would hold {post_position_count} positions "
                    f"(limit {profile.max_positions})"
                ),
            }
        )

    return {"allowed": len(violations) == 0, "violations": violations}


# ── Circuit breaker ──────────────────────────────────────────────────────────


def circuit_breaker_status(
    vix: float | None,
    monthly_drawdown_pct: float | None,
    profile: RiskProfile,
) -> dict[str, Any]:
    """Two-leg breaker: VIX above threshold OR monthly drawdown beyond limit.

    ``monthly_drawdown_pct`` is a positive magnitude (18 means the book is down
    18% on the month). Either leg trips. A ``None`` input means "no data for this
    leg" — that leg simply does not contribute (the skill should treat missing
    VIX/NAV history as a data-sufficiency problem, handled elsewhere).
    """
    legs: list[dict[str, Any]] = []

    if vix is not None and vix > profile.circuit_breaker.vix_threshold:
        legs.append(
            {
                "leg": "vix",
                "value": vix,
                "threshold": profile.circuit_breaker.vix_threshold,
                "message": f"VIX {vix:g} above {profile.circuit_breaker.vix_threshold:g}",
            }
        )

    if (
        monthly_drawdown_pct is not None
        and monthly_drawdown_pct > profile.circuit_breaker.monthly_drawdown_pct
    ):
        legs.append(
            {
                "leg": "monthly_drawdown",
                "value": monthly_drawdown_pct,
                "threshold": profile.circuit_breaker.monthly_drawdown_pct,
                "message": (
                    f"monthly drawdown {monthly_drawdown_pct:g}% beyond "
                    f"{profile.circuit_breaker.monthly_drawdown_pct:g}%"
                ),
            }
        )

    return {"tripped": len(legs) > 0, "legs": legs}


# ── Autonomy ceilings (the critical §B fixes) ────────────────────────────────


def cumulative_ceiling_check(
    baseline_nav_start_of_day: float,
    spent_today: float,
    trades_today: int,
    candidate_value: float,
    profile: RiskProfile,
) -> dict[str, Any]:
    """The §B drain fix: a CUMULATIVE 24h ceiling anchored to a FIXED baseline.

    The per-run cap (33% of NAV) does NOT stop a `/loop` from draining the
    account: each round mobilizes 33% of what is LEFT, so a dozen rounds empty
    it while every single round looks "within the per-run cap". The defense is a
    ceiling that is cumulative over a rolling 24h window and anchored to a FIXED
    start-of-day NAV — a shrinking account must not re-authorize fresh slices.

    The caller is responsible for the anchoring discipline: pass the SAME
    ``baseline_nav_start_of_day`` for every round in the window, and the running
    ``spent_today`` / ``trades_today`` from the persistent decision log (so the
    ceiling survives restarts and loops). This function holds the arithmetic;
    memory.py holds the state.

    Two independent ceilings, both must pass:
      * value: ``spent_today + candidate_value <= daily_cumulative_pct% of baseline``
      * count: ``trades_today + 1 <= daily_max_trades``
    """
    if baseline_nav_start_of_day <= 0:
        raise ValueError("baseline_nav_start_of_day must be positive")
    if candidate_value < 0:
        raise ValueError("candidate_value must be non-negative")

    budget = baseline_nav_start_of_day * profile.autonomy.daily_cumulative_pct / PERCENT
    remaining_budget = max(0.0, budget - spent_today)

    value_ok = spent_today + candidate_value <= budget + LIMIT_EPSILON
    count_ok = trades_today + 1 <= profile.autonomy.daily_max_trades

    if not value_ok:
        reason = (
            f"daily value ceiling: spending {candidate_value:g} would push today's "
            f"total to {spent_today + candidate_value:g}, over the "
            f"{profile.autonomy.daily_cumulative_pct:g}% budget of "
            f"{budget:g} (baseline NAV {baseline_nav_start_of_day:g})"
        )
    elif not count_ok:
        reason = (
            f"daily trade-count ceiling: this would be trade "
            f"{trades_today + 1} of a {profile.autonomy.daily_max_trades} max"
        )
    else:
        reason = (
            f"within daily budget: {remaining_budget:g} of {budget:g} left, "
            f"trade {trades_today + 1} of {profile.autonomy.daily_max_trades}"
        )

    return {
        "allowed": value_ok and count_ok,
        "reason": reason,
        "remaining_budget": remaining_budget,
        "budget": budget,
        "spent_today": spent_today,
        "trades_today": trades_today,
        "daily_max_trades": profile.autonomy.daily_max_trades,
    }


def per_run_ceiling_check(
    baseline_nav_start_of_day: float,
    spent_this_run: float,
    trades_this_run: int,
    candidate_value: float,
    profile: RiskProfile,
) -> dict[str, Any]:
    """The §F per-run cap: ``<= per_run_pct of NAV`` OR ``<= per_run_max_trades``
    per autonomous round, whichever binds first.

    Sits BELOW the daily cumulative ceiling: within a single run the per-run cap
    (33% of NAV) binds before the daily one (50%). The denominator is the FIXED
    day baseline -- the SAME anchor as the daily ceiling, a deliberate hardening
    over current-NAV so a falling account never widens the per-run budget. The
    per-run totals come from the decision log since the run marker (begin_run);
    this function holds only the arithmetic.
    """
    if baseline_nav_start_of_day <= 0:
        raise ValueError("baseline_nav_start_of_day must be positive")
    if candidate_value < 0:
        raise ValueError("candidate_value must be non-negative")

    budget = baseline_nav_start_of_day * profile.autonomy.per_run_pct / PERCENT
    remaining_budget = max(0.0, budget - spent_this_run)

    value_ok = spent_this_run + candidate_value <= budget + LIMIT_EPSILON
    count_ok = trades_this_run + 1 <= profile.autonomy.per_run_max_trades

    if not value_ok:
        reason = (
            f"per-run value ceiling: spending {candidate_value:g} would push this "
            f"run's total to {spent_this_run + candidate_value:g}, over the "
            f"{profile.autonomy.per_run_pct:g}% per-run budget of {budget:g} "
            f"(baseline NAV {baseline_nav_start_of_day:g})"
        )
    elif not count_ok:
        reason = (
            f"per-run trade-count ceiling: this would be trade "
            f"{trades_this_run + 1} of a {profile.autonomy.per_run_max_trades} max per run"
        )
    else:
        reason = (
            f"within per-run budget: {remaining_budget:g} of {budget:g} left, "
            f"trade {trades_this_run + 1} of {profile.autonomy.per_run_max_trades}"
        )

    return {
        "allowed": value_ok and count_ok,
        "reason": reason,
        "remaining_budget": remaining_budget,
        "budget": budget,
        "spent_this_run": spent_this_run,
        "trades_this_run": trades_this_run,
        "per_run_max_trades": profile.autonomy.per_run_max_trades,
    }


def drawdown_kill_check(
    nav_at_loop_start: float,
    current_nav: float,
    drawdown_kill_pct: float,
) -> dict[str, Any]:
    """Disarm autonomy if drawdown since loop start hits the kill threshold.

    Gains produce 0 drawdown (never negative). On a kill the skill must DISARM
    autonomy and require a manual re-arm — never auto-continue (spec, §B).
    Fires at or beyond the threshold (``>=``): a kill switch should not let the
    exact threshold slip through.
    """
    if nav_at_loop_start <= 0:
        raise ValueError("nav_at_loop_start must be positive")

    drawdown_pct = max(0.0, (nav_at_loop_start - current_nav) / nav_at_loop_start * PERCENT)
    kill = drawdown_pct >= drawdown_kill_pct
    return {
        "kill": kill,
        "drawdown_pct": drawdown_pct,
        "threshold_pct": drawdown_kill_pct,
        "action": "disarm_autonomy_require_manual_rearm" if kill else "continue",
    }
