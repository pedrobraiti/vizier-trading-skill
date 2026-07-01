"""Command-line interface — the skill's bridge to the deterministic core.

The skill invokes ``python -m vizier <command> --json '<payload>'`` from Bash and
reads back a single-line JSON envelope ``{"ok": bool, "data"|"error": ...}`` that
mirrors the MCP envelope. Every helper is exposed as a subcommand; the payload
carries that helper's arguments as a JSON object.

Example::

    python -m vizier ceiling --json '{"baseline_nav_start_of_day": 1000,
        "spent_today": 480, "trades_today": 3, "candidate_value": 50}'
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from . import autonomy, data_sufficiency, memory, reconcile, risk, scorecard

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_PROFILE_PATH = os.environ.get(
    "VIZIER_PROFILE_PATH", str(_PACKAGE_ROOT / "config" / "risk_profile.yaml")
)
_DEFAULT_MEMORY_DIR = os.environ.get("VIZIER_MEMORY_DIR", str(memory.DEFAULT_MEMORY_DIR))


def _profile(args: argparse.Namespace) -> risk.RiskProfile:
    return risk.load_profile(args.profile_path, args.profile_name)


# ── Command handlers ─────────────────────────────────────────────────────────
# Each takes the parsed JSON payload plus the CLI args and returns a plain,
# JSON-serializable result (the envelope wrapping happens in `main`).


def _cmd_profile(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return _profile(args).to_dict()


def _cmd_size(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return risk.position_size(
        slot_base=payload["slot_base"],
        conviction=payload["conviction"],
        nav=payload["nav"],
        profile=_profile(args),
        explicit_order=payload.get("explicit_order", False),
    )


def _cmd_allocate(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    for field in ("total_amount", "candidates", "nav"):
        if field not in payload:
            raise ValueError(f"missing required field: {field}")
    return risk.allocate_across_candidates(
        total_amount=payload["total_amount"],
        candidates=payload["candidates"],
        nav=payload["nav"],
        profile=_profile(args),
        explicit_order=payload.get("explicit_order", False),
        weighting=payload.get("weighting", "conviction"),
        allow_over_cap=payload.get("allow_over_cap", False),
    )


def _cmd_limits(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return risk.check_position_limits(
        portfolio=payload["portfolio"],
        candidate=payload["candidate"],
        profile=_profile(args),
    )


def _cmd_breaker(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return risk.circuit_breaker_status(
        vix=payload.get("vix"),
        monthly_drawdown_pct=payload.get("monthly_drawdown_pct"),
        profile=_profile(args),
    )


def _cmd_ceiling(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return risk.cumulative_ceiling_check(
        baseline_nav_start_of_day=payload["baseline_nav_start_of_day"],
        spent_today=payload["spent_today"],
        trades_today=payload["trades_today"],
        candidate_value=payload["candidate_value"],
        profile=_profile(args),
    )


def _cmd_drawdown_kill(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    threshold = payload.get("drawdown_kill_pct")
    if threshold is None:
        threshold = _profile(args).autonomy.drawdown_kill_pct
    return risk.drawdown_kill_check(
        nav_at_loop_start=payload["nav_at_loop_start"],
        current_nav=payload["current_nav"],
        drawdown_kill_pct=threshold,
    )


def _cmd_write_thesis(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    path = memory.write_thesis(payload, memory_dir=args.memory_dir, commit=args.commit)
    return {"path": str(path)}


def _cmd_read_thesis(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.read_thesis(
        payload["ticker"], payload["open_date"], memory_dir=args.memory_dir
    )


def _cmd_list_theses(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.list_open_theses(memory_dir=args.memory_dir)


def _cmd_close_thesis(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.close_thesis(
        payload["ticker"],
        payload["open_date"],
        close_date=payload["close_date"],
        exit_price=payload["exit_price"],
        realized_pnl=payload["realized_pnl"],
        alpha_vs_spy=payload.get("alpha_vs_spy"),
        lesson=payload.get("lesson"),
        memory_dir=args.memory_dir,
        commit=args.commit,
    )


def _cmd_update_reviewed(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.update_last_reviewed(
        payload["ticker"],
        payload["open_date"],
        reviewed_at=payload.get("reviewed_at"),
        memory_dir=args.memory_dir,
        commit=args.commit,
    )


def _cmd_append_decision(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.append_decision(payload, memory_dir=args.memory_dir, commit=args.commit)


def _cmd_nav_snapshot(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.record_nav_snapshot(
        payload["net_liquidation"],
        timestamp=payload.get("timestamp"),
        available_funds=payload.get("available_funds"),
        currency=payload.get("currency", "USD"),
        venue=payload.get("venue"),
        memory_dir=args.memory_dir,
        commit=args.commit,
    )


def _cmd_drawdown(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.compute_drawdown(
        window_days=payload.get("window_days"),
        venue=payload.get("venue"),
        memory_dir=args.memory_dir,
    )


def _cmd_scorecard(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    if "as_of" not in payload:
        raise ValueError("missing required field: as_of (ISO date, e.g. 2026-07-01)")
    return scorecard.compute_scorecard(
        memory.list_all_theses(memory_dir=args.memory_dir),
        prices=payload.get("prices"),
        benchmarks=payload.get("benchmarks"),
        as_of=payload["as_of"],
        decision_log=memory.read_decision_log(memory_dir=args.memory_dir),
    )


def _cmd_tranches(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.tranche_balances(payload["ticker"], memory_dir=args.memory_dir)


def _cmd_tranche_sell(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.check_tranche_sell(
        payload["ticker"], payload["tag"], payload["qty"], memory_dir=args.memory_dir
    )


def _cmd_reconcile(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return reconcile.reconcile_exposure(
        own_sent_orders=payload["own_sent_orders"],
        broker_positions=payload["broker_positions"],
        ticker=payload["ticker"],
        venue=payload.get("venue"),
        lag_window_seconds=payload.get("lag_window_seconds"),
    )


def _cmd_build_own_sent_orders(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.build_own_sent_orders(
        ticker=payload.get("ticker"),
        now=payload.get("now"),
        within_seconds=payload.get("within_seconds"),
        memory_dir=args.memory_dir,
    )


def _cmd_trim_qty(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    result = risk.trim_quantity(
        current_qty=payload.get("current_qty"),
        pct=payload.get("pct"),
        current_price=payload.get("current_price"),
        dollar_amount=payload.get("dollar_amount"),
        step=payload.get("step"),
    )
    # Optional tranche cross-check: confirm the computed qty fits the named tag's
    # tranche (so a tactical trim cannot eat the core) without a second call.
    tag = payload.get("tag")
    ticker = payload.get("ticker")
    if tag is not None and ticker is not None:
        result["tranche_check"] = memory.check_tranche_sell(
            ticker, tag, result["qty"], memory_dir=args.memory_dir
        )
    return result


def _cmd_provenance(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return reconcile.position_provenance(payload["ticker"], payload["theses"])


def _cmd_data_sufficiency(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return data_sufficiency.assess(payload["scout_responses"], payload["decision_type"])


def _cmd_arm_autonomy(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.arm_autonomy(
        payload["nav"],
        now=payload.get("now"),
        memory_dir=args.memory_dir,
        commit=args.commit,
    )


def _cmd_disarm_autonomy(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.disarm_autonomy(
        now=payload.get("now"),
        reason=payload.get("reason"),
        memory_dir=args.memory_dir,
        commit=args.commit,
    )


def _cmd_begin_run(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return memory.begin_run(
        now=payload.get("now"), memory_dir=args.memory_dir, commit=args.commit
    )


def _cmd_autonomy_state(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    day = memory.autonomy_day_state(now=payload.get("now"), memory_dir=args.memory_dir)
    run = memory.autonomy_run_state(now=payload.get("now"), memory_dir=args.memory_dir)
    return {**day, "run": run}


def _cmd_autonomy_gate(payload: dict[str, Any], args: argparse.Namespace) -> Any:
    return autonomy.check_autonomy_gate(
        candidate_value=payload["candidate_value"],
        current_nav=payload["current_nav"],
        profile=_profile(args),
        now=payload.get("now"),
        memory_dir=args.memory_dir,
    )


COMMANDS = {
    "profile": _cmd_profile,
    "size": _cmd_size,
    "allocate": _cmd_allocate,
    "limits": _cmd_limits,
    "breaker": _cmd_breaker,
    "ceiling": _cmd_ceiling,
    "drawdown-kill": _cmd_drawdown_kill,
    "write-thesis": _cmd_write_thesis,
    "read-thesis": _cmd_read_thesis,
    "list-theses": _cmd_list_theses,
    "close-thesis": _cmd_close_thesis,
    "update-reviewed": _cmd_update_reviewed,
    "append-decision": _cmd_append_decision,
    "nav-snapshot": _cmd_nav_snapshot,
    "drawdown": _cmd_drawdown,
    "scorecard": _cmd_scorecard,
    "tranches": _cmd_tranches,
    "tranche-sell": _cmd_tranche_sell,
    "reconcile": _cmd_reconcile,
    "build-own-sent-orders": _cmd_build_own_sent_orders,
    "trim-qty": _cmd_trim_qty,
    "provenance": _cmd_provenance,
    "data-sufficiency": _cmd_data_sufficiency,
    "arm-autonomy": _cmd_arm_autonomy,
    "disarm-autonomy": _cmd_disarm_autonomy,
    "begin-run": _cmd_begin_run,
    "autonomy-state": _cmd_autonomy_state,
    "autonomy-gate": _cmd_autonomy_gate,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vizier",
        description="Vizier deterministic core - JSON-in / JSON-envelope-out.",
    )
    parser.add_argument("command", choices=sorted(COMMANDS), help="helper to run")
    parser.add_argument(
        "--json",
        default="{}",
        dest="payload",
        help="JSON object with the command's arguments",
    )
    parser.add_argument("--profile-path", default=_DEFAULT_PROFILE_PATH)
    parser.add_argument("--profile-name", default=None)
    parser.add_argument("--memory-dir", default=_DEFAULT_MEMORY_DIR)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="commit the memory's private git repo after a write",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            raise ValueError("--json payload must be a JSON object")
        data = COMMANDS[args.command](payload, args)
        # ensure_ascii=True so any non-ASCII in a data value is escaped to \uXXXX
        # rather than crashing on a non-UTF-8 console (e.g. Windows cp1252, where
        # the skill invokes this via Bash). Valid JSON either way.
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=True, default=str))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report, never crash raw
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
                ensure_ascii=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
