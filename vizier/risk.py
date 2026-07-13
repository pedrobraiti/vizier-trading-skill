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

from .constants import (
    LIMIT_EPSILON,
    MAX_CONVICTION,
    PERCENT,
    QTY_CASH_CONSISTENCY_TOLERANCE,
    QTY_CASH_UNIT_PRICE_RATIO,
    QTY_EPSILON,
)

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
    conviction_full_size_pct_of_cap: float
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
        # Default 65 so an older config without the knob still loads and sizes
        # sanely (conviction-5 at 65% of the per-asset cap).
        conviction_full_size_pct_of_cap=float(
            body.get("conviction_full_size_pct_of_cap", 65)
        ),
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


# Weighting modes the allocator officially supports. An explicit per-candidate
# ``weight`` overrides the mode, but the mode string itself is still validated so
# a garbage value never passes silently (it would otherwise be ignored when
# weights are present, yet rejected when they are not — an inconsistency).
_ALLOWED_WEIGHTINGS = ("conviction", "equal")


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
    """Linear conviction sizing scaled by the conviction-full-size knob:
    ``slot_base * (conviction/5) * (conviction_full_size_pct_of_cap/100)``, then
    hard-capped at the per-asset NAV limit.

    With ``slot_base`` set to the per-asset cap (the deterministic default), a
    max-conviction (5) position targets ``cap * conviction_full_size_pct_of_cap/100``
    — a configured FRACTION of the cap (default 65%), leaving headroom — while the
    per-asset cap stays the hard ceiling on the result.

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

    full_size_fraction = profile.conviction_full_size_pct_of_cap / PERCENT
    raw = slot_base * (conviction / MAX_CONVICTION) * full_size_fraction
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


def _candidate_is_eligible(
    candidate: dict[str, Any], profile: RiskProfile, *, explicit_order: bool
) -> bool:
    """A candidate clears the conviction floor when the WHOLE call is explicit,
    when THIS leg is flagged ``explicit`` (a user-named leg in a mixed request),
    or when its conviction is at/above the floor on its own merit."""
    return (
        explicit_order
        or bool(candidate.get("explicit", False))
        or candidate.get("conviction", 0) >= profile.conviction_floor
    )


def _allocation_weights(
    candidates: list[dict[str, Any]], weighting: str
) -> tuple[list[float], float]:
    """Per-candidate raw weights for the chosen mode, plus their sum.

    An explicit per-candidate ``weight`` on ANY leg switches to explicit-weights
    mode (honor exactly what the caller asked). Otherwise ``weighting`` selects
    ``conviction`` (default — proportional to conviction) or ``equal`` (an even
    split the caller asked for instead of silently conviction-weighting).
    """
    if any("weight" in c for c in candidates):
        weights = [float(c.get("weight", 0.0)) for c in candidates]
    elif weighting == "equal":
        weights = [1.0 for _ in candidates]
    elif weighting == "conviction":
        weights = [float(c.get("conviction", 0)) for c in candidates]
    else:
        raise ValueError(f"unknown weighting '{weighting}'; use 'conviction' or 'equal'")
    return weights, sum(weights)


def allocate_across_candidates(
    total_amount: float,
    candidates: list[dict[str, Any]],
    nav: float,
    profile: RiskProfile,
    *,
    explicit_order: bool = False,
    weighting: str = "conviction",
    allow_over_cap: bool = False,
) -> dict[str, Any]:
    """Split a fixed budget across candidates, then cap each leg at the per-asset
    NAV limit. Whatever the caps shave off is reported as ``unallocated`` rather
    than silently forced back in (forcing it could re-breach a cap).

    Weighting (spec, §F "$100 em 3"):
      * ``conviction`` (default) — ``Allocation_i = total * conviction_i / Σconviction``.
      * ``equal`` — even split; honor an explicit "split equally across these"
        instead of silently conviction-weighting.
      * explicit per-candidate ``weight`` on any leg — honor those weights exactly
        (overrides ``weighting``).

    Conviction floor: a candidate below the floor is dropped UNLESS the whole call
    is ``explicit_order`` OR that leg carries ``"explicit": true``. The per-candidate
    flag is what lets a MIXED request work — a user-named sub-floor leg is kept while
    skill-derived sub-floor legs in the same call are still dropped (spec, §10 + §E).

    ``allow_over_cap`` (default False) is the faithful-execution escape hatch: a fixed
    dollar amount the user named explicitly is a CONTRACT, just like a read-only
    recommendation count is. By default the per-asset cap still shaves an over-cap leg
    (Vizier-CHOSEN sizing must respect the limit). But when the user gives an explicit
    dollar order that the cap would clamp, the skill warns ONCE (the single risk
    caution) and, if the user confirms, re-runs with ``allow_over_cap=True`` — then the
    full amount is deployed and each over-cap leg is flagged ``over_cap`` for honest
    disclosure rather than silently left ``unallocated``. The limit is a caution to a
    human at the wheel here, not a refusal (it regains teeth under armed autonomy,
    where the human is not the backstop).

    Each candidate dict needs at least ``ticker`` and ``conviction``; optional
    ``explicit`` (bool) and ``weight`` (float) per leg.
    """
    if not math.isfinite(nav) or nav <= 0:
        raise ValueError("nav must be positive")
    if not math.isfinite(total_amount) or total_amount < 0:
        raise ValueError("total_amount must be non-negative and finite")
    # Validate the weighting mode UP FRONT — regardless of whether per-candidate
    # weights are present — so a garbage value ("momentum") fails loudly instead
    # of being silently ignored on the explicit-weights path.
    if weighting not in _ALLOWED_WEIGHTINGS:
        raise ValueError(f"unknown weighting '{weighting}'; use 'conviction' or 'equal'")
    # A buy-allocation must never produce a negative (short) size: reject negative
    # per-candidate weight/conviction here, the same way negative total_amount and
    # nav<=0 are rejected above. Zero is allowed (it simply gets nothing).
    for candidate in candidates:
        if "weight" in candidate:
            weight_value = float(candidate["weight"])
            if not math.isfinite(weight_value) or weight_value < 0:
                raise ValueError("weight must be non-negative and finite")
        conviction_value = float(candidate.get("conviction", 0))
        if not math.isfinite(conviction_value) or conviction_value < 0:
            raise ValueError("conviction must be non-negative and finite")
    # Weights are all-or-none: ANY weight switches the whole call to explicit-weights
    # mode, where a leg without one would default to 0 and silently receive $0 — the
    # same silent-unfaithfulness class as the degenerate-basis $0 deploy. Fail loudly
    # instead; the caller either weights every leg or none.
    weighted_count = sum(1 for c in candidates if "weight" in c)
    if 0 < weighted_count < len(candidates):
        raise ValueError(
            "when any candidate carries 'weight', every candidate must — a leg without "
            "one would silently be allocated $0 in explicit-weights mode"
        )

    eligible = [
        c for c in candidates if _candidate_is_eligible(c, profile, explicit_order=explicit_order)
    ]
    skipped = [
        c["ticker"]
        for c in candidates
        if not _candidate_is_eligible(c, profile, explicit_order=explicit_order)
    ]

    weights, weight_sum = _allocation_weights(eligible, weighting)
    # An explicit positive budget is a CONTRACT: it must deploy. If the chosen basis is
    # degenerate (all weights/convictions zero -> weight_sum 0), an even split is the
    # faithful recovery — deploy what the user asked rather than silently leaving the
    # whole amount unallocated. Flagged so the recovery is never silent.
    weight_fallback = False
    if total_amount > 0 and eligible and weight_sum <= 0:
        weights = [1.0 for _ in eligible]
        weight_sum = float(len(eligible))
        weight_fallback = True
    cap = _asset_cap(nav, profile)

    allocations: list[dict[str, Any]] = []
    allocated_total = 0.0
    for c, raw_weight in zip(eligible, weights, strict=True):
        weight = (raw_weight / weight_sum) if weight_sum > 0 else 0.0
        target = total_amount * weight
        over_cap = target > cap + LIMIT_EPSILON
        # Default: shave an over-cap leg to the per-asset cap. allow_over_cap: deploy
        # the full explicit amount (honored as a contract after a single confirm) and
        # disclose the breach via ``over_cap`` instead of silently leaving it unallocated.
        size = target if allow_over_cap else min(target, cap)
        allocated_total += size
        allocations.append(
            {
                "ticker": c["ticker"],
                "conviction": c.get("conviction", 0),
                "target": target,
                "size": size,
                "capped": size < target - LIMIT_EPSILON,  # was the leg actually shaved?
                "over_cap": over_cap,  # did the target exceed the per-asset cap (risk signal)?
            }
        )

    return {
        "allocations": allocations,
        "skipped": skipped,
        "allocated_total": allocated_total,
        "unallocated": max(0.0, total_amount - allocated_total),
        "asset_cap": cap,
        "allow_over_cap": allow_over_cap,
        "weight_fallback": weight_fallback,
        "weighting": "explicit" if any("weight" in c for c in eligible) else weighting,
    }


# ── Sell-side sizing: %/$ -> base quantity ───────────────────────────────────
#
# UNITS, once and for all (the whole class of bug here is an implicit unit):
#   * ``qty`` / ``quantity`` / ``current_qty`` / ``position_qty`` = SHARES or crypto
#     BASE UNITS. This is the ONLY unit any exit (sell, stop, trim, close) is sized in.
#   * ``cash_amount`` / ``dollar_amount`` / ``cash_qty`` / ``filled_cash`` = USD.
# An exit sized off a dollar figure is a naked short waiting to happen: a real
# IBKR fill of `buy(AAPL, cash_amount=2)` acquired 0.0063 shares, and a SELL stop
# for "2" against it would have been a ~317x oversell.


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
      * **pct**    — ``current_qty * pct/100``.
      * **dollar** — ``dollar_amount / current_price`` (USD in, SHARES out).

    ``current_qty`` — the live held quantity in SHARES/base units, read from the
    venue's ``positions`` tool — is **required in both modes**: it is the hard
    ceiling that makes an oversell impossible. A dollar trim with no holding to
    cap against could size an exit larger than the position (the naked-short
    failure mode), so it is refused rather than computed.

    The result is capped at ``current_qty`` (never oversell) and floored to ``step``
    (the market lot size / amount precision) when given — always DOWN, so a rounding
    error can only sell slightly less, never more than intended.
    """
    if pct is not None and dollar_amount is not None:
        raise ValueError("pass pct OR dollar_amount, not both")
    if current_qty is None:
        raise ValueError(
            "trim needs current_qty - the live held quantity in SHARES/base units "
            "(from the venue's `positions` tool). It is the ceiling that makes an "
            "oversell impossible; without it a trim could size an exit larger than "
            "the position"
        )
    if not math.isfinite(float(current_qty)) or float(current_qty) < 0:
        raise ValueError("current_qty must be a non-negative, finite quantity (shares/base units)")

    if pct is not None:
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
        raise ValueError("provide pct or dollar_amount (with current_price), plus current_qty")

    capped_to_holding = False
    if raw > current_qty:
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

    # A non-zero requested trim that floors to 0 means the trim is smaller than
    # one market lot: it would send a zero-qty exit and the intended risk
    # reduction silently does NOT happen. Flag it so the skill can surface it
    # rather than treating a no-op as a successful trim.
    below_min_lot = qty == 0.0 and raw > LIMIT_EPSILON

    return {
        "qty": qty,
        "raw_qty": raw,
        "mode": mode,
        "step": step,
        "rounded_down": qty < raw - LIMIT_EPSILON,
        "capped_to_holding": capped_to_holding,
        "below_min_lot": below_min_lot,
    }


# ── Fill-unit cross-check: is this number SHARES or DOLLARS? ─────────────────


def check_fill_units(
    *,
    qty: float | None,
    cash_qty: float | None,
    entry_price: float | None,
) -> dict[str, Any]:
    """Is ``qty`` really a SHARE count, or a dollar amount wearing a share's name?

    The signature of the bug (found live, 2026-07-13): a US$ "cash quantity" buy
    of $2 of AAPL filled 0.0063 shares @ $317.25, and IBKR reported the cumulative
    fill of that order as ``filled_quantity = 2.0`` — the DOLLARS, not the shares.
    Written into a thesis as ``qty``, that number corrupts the tranche guard, the
    P&L and the scorecard, and sizes a 2-share stop against a 0.0063-share holding.

    The check: with ``cash_qty`` (USD deployed) and ``entry_price`` (USD/share)
    both known, the shares those dollars *could* have bought is
    ``cash_qty / entry_price``. A ``qty`` that instead equals ``cash_qty`` while
    the price sits far from $1 is dollars-as-shares — conclusive, not a heuristic.

    Returns ``consistent`` (qty agrees with the implied share count within the
    tolerance), ``unit_error`` (the conclusive dollars-as-shares signature) and
    the numbers behind the verdict. Silent when there is nothing to compare
    against (``cash_qty`` is None for a share-sized order — then no cross-check
    is possible and none is claimed).
    """
    if qty is None or cash_qty is None or entry_price is None:
        return {"checked": False, "consistent": None, "unit_error": False, "reason": None}

    qty_value = float(qty)
    cash_value = float(cash_qty)
    price = float(entry_price)
    if price <= 0 or cash_value <= 0 or qty_value <= 0:
        return {"checked": False, "consistent": None, "unit_error": False, "reason": None}

    implied_qty = cash_value / price
    ratio = qty_value / implied_qty
    consistent = abs(ratio - 1.0) <= QTY_CASH_CONSISTENCY_TOLERANCE

    # Dollars-as-shares: qty *is* the cash figure, and the price is far enough
    # from $1 that the two units cannot legitimately coincide.
    looks_like_cash = (
        math.isclose(qty_value, cash_value, rel_tol=1e-6)
        and (price > QTY_CASH_UNIT_PRICE_RATIO or price < 1.0 / QTY_CASH_UNIT_PRICE_RATIO)
    )

    reason = None
    if looks_like_cash:
        reason = (
            f"qty {qty_value:g} equals the cash amount {cash_value:g} while the price is "
            f"{price:g}/share - this is DOLLARS recorded as SHARES (IBKR reports a "
            f"cash-quantity order's filled_quantity in dollars). The real quantity is "
            f"~{implied_qty:g} shares; read it from the venue's `positions` tool"
        )
    elif not consistent:
        reason = (
            f"qty {qty_value:g} does not match cash_qty/entry_price = {implied_qty:g} "
            f"({ratio:.3g}x) - reconcile the fill against `positions` before journaling"
        )

    return {
        "checked": True,
        "consistent": consistent,
        "unit_error": looks_like_cash,
        "implied_qty": implied_qty,
        "ratio": ratio,
        "reason": reason,
    }


# ── Exit sizing: an exit can NEVER exceed what is actually held ──────────────


def exit_quantity(
    *,
    position_qty: float | None,
    requested_qty: float | None = None,
    filled_quantity: float | None = None,
    filled_quantity_is_estimate: bool = False,
    filled_cash: float | None = None,
    is_cash_quantity: bool = False,
    step: float | None = None,
) -> dict[str, Any]:
    """Size any exit (protective stop, trim, sell) so it CANNOT exceed the holding.

    This is the seam that makes the oversize-exit bug unrepresentable rather than
    merely discouraged. The authoritative quantity you hold is ``position_qty`` —
    the exact base/share figure from the venue's ``positions`` tool (it returned
    ``0.0063`` for the live $2 AAPL fill whose ``filled_quantity`` said ``2.0``).
    Everything else is an input to be cross-checked *against* it, never a source
    of truth to size from:

    * ``position_qty`` is **required and is a hard ceiling.** No argument, flag or
      combination of inputs can make the returned ``qty`` exceed it. Absent or
      non-positive → the call is REFUSED (raises): a stop placed with no resolved
      position is a naked short.
    * ``requested_qty`` — the quantity this exit is *meant* to cover (typically the
      lot that just filled, so a fresh tactical stop does not also stop out a
      pre-existing core lot). Defaults to the fill, then to the whole position.
    * ``filled_quantity`` — from ``order_status``. Used only as an intent hint and a
      cross-check. On IBKR a **cash-quantity order historically reported it in
      DOLLARS**, and even corrected it may be an estimate on a partial cash fill —
      so it is never trusted as a ceiling. A ``filled_quantity`` above the held
      position is reported (``filled_quantity_exceeds_position``) and ignored.
    * ``step`` — lot size / amount precision. Rounds **DOWN**, so rounding can only
      ever exit less than intended, never more.

    ``full_exit`` tells the caller the exit covers the whole position — prefer the
    venue's ``close_position`` there, which resolves the exact fractional quantity
    itself instead of round-tripping a float.
    """
    if position_qty is None:
        raise ValueError(
            "exit sizing needs position_qty - the EXACT held quantity in shares/base "
            "units from the venue's `positions` tool. An exit sized from an order fill "
            "alone can exceed the position: IBKR reports a cash-quantity order's "
            "filled_quantity in DOLLARS (a $2 AAPL buy reported 2.0 against a holding "
            "of 0.0063 shares)"
        )
    held = float(position_qty)
    if not math.isfinite(held):
        raise ValueError("position_qty must be a finite quantity (shares/base units)")
    if held <= 0:
        raise ValueError(
            f"no position to exit (position_qty={held:g}) - refusing to size an exit. "
            "Selling/stopping what you do not hold is a naked short; resolve the "
            "position with `positions` first (it may simply lag the fill by 30-45s)"
        )

    warnings: list[str] = []

    fill_exceeds_position = (
        filled_quantity is not None
        and math.isfinite(float(filled_quantity))
        and float(filled_quantity) > held + QTY_EPSILON
    )
    if fill_exceeds_position:
        cash_hint = (
            " This is the signature of a cash-quantity (US$) order whose fill is "
            "reported in DOLLARS."
            if is_cash_quantity or filled_cash is not None
            else ""
        )
        warnings.append(
            f"filled_quantity ({float(filled_quantity):g}) exceeds the held position "
            f"({held:g}) - it is NOT a share count you can size an exit from.{cash_hint} "
            "Sized from `positions` instead."
        )
    if filled_quantity_is_estimate:
        warnings.append(
            "filled_quantity is flagged as an ESTIMATE (a partial fill of a cash-quantity "
            "order does not yield an exact share count) - used only as a cross-check, "
            "the exit is sized from `positions`."
        )

    fill_is_usable = (
        filled_quantity is not None
        and math.isfinite(float(filled_quantity))
        and float(filled_quantity) > 0
        and not fill_exceeds_position
        and not filled_quantity_is_estimate
    )

    if requested_qty is not None:
        intended = float(requested_qty)
        if not math.isfinite(intended) or intended <= 0:
            raise ValueError("requested_qty must be a positive, finite quantity when given")
        intent_source = "requested_qty"
    elif fill_is_usable:
        intended = float(filled_quantity)
        intent_source = "filled_quantity"
    else:
        intended = held
        intent_source = "position_qty"

    # THE ceiling. Everything above is intent; this line is the guarantee.
    capped_to_position = intended > held + QTY_EPSILON
    raw = min(intended, held)

    qty = raw
    if step is not None:
        if step <= 0:
            raise ValueError("step must be positive")
        # Floor to whole steps (kill float dust first, never round UP past `raw`).
        units = math.floor(round(raw / step, 9))
        qty = round(units * step, 12)

    below_min_lot = qty == 0.0 and raw > QTY_EPSILON
    if below_min_lot:
        warnings.append(
            f"the exit rounds to 0 at step {step:g} - it is smaller than one lot, so no "
            "order can be sent and the intended protection does NOT exist. Surface this."
        )
    if capped_to_position:
        warnings.append(
            f"requested exit ({intended:g}) exceeded the held position ({held:g}) and was "
            "capped - an exit is never allowed to be larger than the holding."
        )

    full_exit = qty >= held - QTY_EPSILON

    return {
        "qty": qty,
        "position_qty": held,
        "intended_qty": intended,
        "intent_source": intent_source,
        "capped_to_position": capped_to_position,
        "rounded_down": qty < raw - QTY_EPSILON,
        "below_min_lot": below_min_lot,
        "full_exit": full_exit,
        "filled_quantity_trusted": fill_is_usable,
        "filled_quantity_exceeds_position": fill_exceeds_position,
        "recommended_tool": "close_position" if full_exit else "stop_order/sell",
        "warnings": warnings,
        "unit": "shares/base units (NEVER dollars)",
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
