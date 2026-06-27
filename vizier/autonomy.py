"""The one composed autonomy gate the skill cannot get wrong (spec, §B).

This is the seam-closer. ``cumulative_ceiling_check`` and ``drawdown_kill_check``
in ``risk`` are pure arithmetic; on their own they are only as safe as whatever
feeds them. If the skill (the LLM) had to assemble the fixed day baseline and
re-sum the decision log by hand each round, it would eventually forget — the
exact "forget between rounds" failure the hybrid design exists to prevent.

``check_autonomy_gate`` removes that responsibility from the LLM: it pulls the
day-state from CODE (fixed baseline + decision-log-sourced running totals),
asserts autonomy is armed and unexpired, then runs BOTH the cumulative ceiling
and the drawdown kill, and returns ONE verdict. The skill calls this single
function per candidate and never threads the numbers itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import memory
from .risk import (
    RiskProfile,
    cumulative_ceiling_check,
    drawdown_kill_check,
    per_run_ceiling_check,
)


def check_autonomy_gate(
    candidate_value: float,
    current_nav: float,
    *,
    profile: RiskProfile,
    now: datetime | str | None = None,
    memory_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Single per-candidate autonomy verdict.

    Returns ``{allowed, blocks, ...}`` where ``blocks`` is empty iff allowed.
    The gate composes four independent legs, in this order:

      * ``not_armed``          — autonomy is off or the 24h window expired.
      * ``per_run_ceiling``    — would breach the per-run cap (per_run_pct of the
        FIXED baseline OR per_run_max_trades), measured since the run marker;
        also fires if NO run has begun (the skill must call begin-run first, or
        there is no per-run safety).
      * ``cumulative_ceiling`` — would breach the daily value or trade-count cap,
        against the same FIXED start-of-day baseline (the §B drain fix).
      * ``drawdown_kill``      — NAV has fallen too far from the day baseline;
        autonomy must disarm and require a manual re-arm.

    Per-run and daily totals both come from the decision log, by code — the LLM
    never threads them. ``now`` and ``memory_dir`` are injectable for
    deterministic, offline tests.
    """
    moment = now if now is not None else datetime.now(UTC)
    mem_kwargs: dict[str, Any] = {} if memory_dir is None else {"memory_dir": memory_dir}

    state = memory.autonomy_day_state(now=moment, **mem_kwargs)
    if not state["armed"]:
        return {
            "allowed": False,
            "blocks": [
                {
                    "type": "not_armed",
                    "message": (
                        f"{state['reason']}; the user must arm autonomy "
                        "(arm-autonomy) before any autonomous order"
                    ),
                }
            ],
            "armed": False,
            "baseline_nav": None,
            "spent_today": state["spent_today"],
            "trades_today": state["trades_today"],
            "remaining_budget": 0.0,
            "run_active": False,
            "spent_this_run": 0.0,
            "trades_this_run": 0,
            "per_run_remaining_budget": 0.0,
            "drawdown_pct": None,
            "kill": None,
        }

    baseline = state["baseline_nav"]
    run = memory.autonomy_run_state(now=moment, **mem_kwargs)

    blocks: list[dict[str, Any]] = []

    if not run["run_active"]:
        per_run = None
        blocks.append(
            {
                "type": "per_run_ceiling",
                "message": (
                    "no active run; call begin-run at the start of the autonomous "
                    "round before gating candidates (without it there is no per-run safety)"
                ),
            }
        )
    else:
        per_run = per_run_ceiling_check(
            baseline_nav_start_of_day=baseline,
            spent_this_run=run["spent_this_run"],
            trades_this_run=run["trades_this_run"],
            candidate_value=candidate_value,
            profile=profile,
        )
        if not per_run["allowed"]:
            blocks.append({"type": "per_run_ceiling", "message": per_run["reason"]})

    ceiling = cumulative_ceiling_check(
        baseline_nav_start_of_day=baseline,
        spent_today=state["spent_today"],
        trades_today=state["trades_today"],
        candidate_value=candidate_value,
        profile=profile,
    )
    if not ceiling["allowed"]:
        blocks.append({"type": "cumulative_ceiling", "message": ceiling["reason"]})

    # The drawdown kill must SELF-LATCH: a live recompute from current_nav alone
    # would re-allow autonomy if NAV dipped to the threshold (gate blocked) but
    # the skill never disarmed and NAV later recovered. So a kill is persisted
    # and, once latched, hard-blocks REGARDLESS of current NAV until an explicit
    # disarm or a fresh arm clears it. The latch resets only on re-arm/disarm.
    kill = drawdown_kill_check(
        nav_at_loop_start=baseline,
        current_nav=current_nav,
        drawdown_kill_pct=profile.autonomy.drawdown_kill_pct,
    )
    latch = memory.drawdown_kill_latch(**mem_kwargs)
    already_latched = latch["killed"]
    if kill["kill"] and not already_latched:
        latch = memory.latch_drawdown_kill(
            f"drawdown {kill['drawdown_pct']:.1f}% >= {kill['threshold_pct']:g}% kill threshold",
            now=moment,
            **mem_kwargs,
        )
        already_latched = True

    killed = kill["kill"] or already_latched
    if killed:
        if kill["kill"]:
            kill_message = (
                f"drawdown {kill['drawdown_pct']:.1f}% >= "
                f"{kill['threshold_pct']:g}% kill threshold; disarm autonomy "
                "and require manual re-arm"
            )
        else:
            kill_message = (
                f"autonomy killed and latched ({latch['killed_reason']}); NAV may "
                "have recovered but autonomy stays killed until an explicit disarm "
                "and fresh re-arm"
            )
        blocks.append({"type": "drawdown_kill", "message": kill_message})

    return {
        "allowed": not blocks,
        "blocks": blocks,
        "armed": True,
        "baseline_nav": baseline,
        "window_start": state["window_start"],
        "spent_today": state["spent_today"],
        "trades_today": state["trades_today"],
        "remaining_budget": ceiling["remaining_budget"],
        "run_active": run["run_active"],
        "spent_this_run": run["spent_this_run"],
        "trades_this_run": run["trades_this_run"],
        "per_run_remaining_budget": per_run["remaining_budget"] if per_run else 0.0,
        "drawdown_pct": kill["drawdown_pct"],
        "kill": killed,
        "kill_latched": already_latched,
    }
