"""Persistent state for the brain: thesis store, decision log, NAV snapshots.

The Scout is stateless by design and the Valet only knows point-in-time
positions (no entry date, no reason, no horizon). Everything that must be
REMEMBERED between sessions lives here, on disk, owned by the skill.

Storage layout (under ``memory/``):
  theses/{ticker}-{open_date}.yaml   one file per thesis
  decision_log.jsonl                 append-only audit of every decision
  nav_snapshots.jsonl                daily net-liquidation series (for drawdown)
  autonomy_state.json                armed/disarmed state + fixed day baseline

Decision-log EXECUTION schema (spec, §B + §F). Any decision that actually moved
money must record what filled, so the autonomy day-state can be reconstructed
from code instead of being re-summed by the LLM each round::

    {
      "timestamp": "<ISO-8601, tz-aware>",
      "intent": "buy AAA",
      "mode": "autonomy" | "confirmation",
      "executed_orders": [
        {"side": "BUY"|"SELL", "ticker": "AAA", "value": 200.0,
         "venue": "ibkr"|"crypto", "order_id": "...",
         "timestamp": "<ISO-8601 fill time>",   # optional but recommended:
         "status": "filled"}                     # makes the row reconcile-ready
      ],
      ...free-form fields (candidates, rationale, blocks, ...)
    }

Only ``side == "BUY"`` ``value`` counts toward the daily spend ceiling (sells and
closes reduce risk, they never consume spend). A "trade" for the daily count is
ANY executed order, buy or sell. Research-only decisions simply omit
``executed_orders`` and contribute nothing to the day-state.

The per-order ``timestamp`` and ``status`` are optional here (the aggregators read
only ``side``/``value`` and ignore extra keys) but RECOMMENDED: ``reconcile``'s
``own_sent_orders`` input needs a per-order ``timestamp`` + ``status``, so journaling
them makes each row directly reconcile-ready instead of having to be reconstructed
(see ``references/execution-mechanics.md``).

PRIVACY: this directory is a SEPARATE, PRIVATE git repo (see ``commit_memory``).
It is gitignored out of the public skill repo so real positions/decisions never
reach GitHub. Only ``EXAMPLE_*`` templates are committed to the public repo.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .constants import AUTONOMY_WINDOW_SECONDS

_LOGGER = logging.getLogger(__name__)

# A git runner is injectable so tests never touch a real repo. It receives the
# git args (without the leading "git") and the cwd; it returns nothing the
# caller depends on. The default shells out to git; tests pass a fake recorder.
GitRunner = Callable[[list[str], Path], Any]

DECISION_LOG_NAME = "decision_log.jsonl"
NAV_SNAPSHOTS_NAME = "nav_snapshots.jsonl"
AUTONOMY_STATE_NAME = "autonomy_state.json"
THESES_DIRNAME = "theses"

# Tags that get tranche accounting (spec, §D multi-horizon contradiction fix).
HORIZON_TAGS = ("core", "tactical")

# Order sides whose value consumes the daily spend ceiling. Buys deploy capital;
# sells/closes reduce risk and never count toward the spend budget.
BUY_SIDES = frozenset({"BUY"})

DEFAULT_MEMORY_DIR = Path(__file__).resolve().parent.parent / "memory"


class AutonomyAlreadyArmedError(RuntimeError):
    """Raised when ``arm_autonomy`` is called while an unexpired window is active.

    Re-arming mid-window would reset ``window_start`` and drop the day's prior
    spend out of the aggregation, freeing a fresh 50%-of-NAV slice — the §B drain
    vector. A legitimate re-arm requires an explicit ``disarm_autonomy`` first
    (the post-kill path disarms, so ``active`` becomes None) or letting the 24h
    window expire. There is deliberately NO force-override: that would be the
    exact footgun this guard exists to remove.
    """


# ── Low-level helpers ────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_dt(value: datetime | str | None) -> datetime | None:
    """Coerce a datetime or ISO-8601 string to a tz-aware datetime (assume UTC
    if naive). Returns None for None so callers can fall back to the live clock."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _safe_ticker(ticker: str) -> str:
    """Make a ticker safe for a filename. Crypto symbols are CCXT ``BASE/QUOTE``
    (e.g. ``BTC/USDT``); the slash would break the path, so it is encoded. The
    real symbol is preserved inside the file's ``ticker`` field."""
    return ticker.replace("/", "_").replace("\\", "_")


def _theses_dir(memory_dir: Path) -> Path:
    return Path(memory_dir) / THESES_DIRNAME


def _thesis_path(ticker: str, open_date: str, memory_dir: Path) -> Path:
    return _theses_dir(memory_dir) / f"{_safe_ticker(ticker)}-{open_date}.yaml"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file, tolerating malformed lines.

    A single corrupt line (e.g. a torn write or a partial flush) must NOT hard-
    break day-state reconstruction: one bad row would otherwise wipe out every
    good decision in the log. Matching the sibling Valet hardening, a line that
    fails to parse is skipped (and logged) and the good lines still aggregate.
    """
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            _LOGGER.warning("skipping malformed JSONL line %d in %s", number, path)
    return entries


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _default_git_runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _maybe_commit(
    message: str,
    *,
    memory_dir: Path,
    commit: bool,
    runner: GitRunner | None,
) -> None:
    if commit:
        commit_memory(message, memory_dir=memory_dir, runner=runner)


# ── Thesis schema ────────────────────────────────────────────────────────────
# Fields per spec §F. `baseline_snapshot` is the critical bit: it freezes the
# quantitative signals at buy time so the thesis-check can diff against them
# (Scout's as_of cannot reconstruct soft signals or historical multiples).

REQUIRED_THESIS_FIELDS = (
    "ticker",
    "horizon_tag",
    "open_date",
    "entry_price",
    "qty",
    "conviction",
    "thesis_one_liner",
    "reason",
    "main_risk",
    "review_trigger",
    "review_trigger_is_hard_stop",
    "baseline_snapshot",
)


def _validate_thesis(thesis: dict[str, Any]) -> None:
    missing = [f for f in REQUIRED_THESIS_FIELDS if f not in thesis]
    if missing:
        raise ValueError(f"thesis missing required fields: {', '.join(missing)}")
    if thesis["horizon_tag"] not in HORIZON_TAGS:
        raise ValueError(
            f"horizon_tag must be one of {HORIZON_TAGS}, got {thesis['horizon_tag']!r}"
        )
    # qty is the filled share/base quantity. It must be a real number, not null:
    # tranche accounting SUMS qty, so a thesis with no qty is silently invisible
    # to the §D core-vs-tactical guard (4/5 sims hit this). Fail loud at write.
    if thesis.get("qty") is None:
        raise ValueError(
            "thesis 'qty' (filled quantity) is required and must not be null - "
            "without it the position is invisible to tranche accounting"
        )


def write_thesis(
    thesis: dict[str, Any],
    *,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
    commit: bool = False,
    runner: GitRunner | None = None,
) -> Path:
    """Persist a thesis to ``theses/{ticker}-{open_date}.yaml``.

    Defaults ``status`` to ``open`` and stamps ``last_reviewed`` with the open
    date if absent. Validates the required §F fields up front so a malformed
    thesis fails loudly rather than corrupting the store.
    """
    memory_dir = Path(memory_dir)
    record = dict(thesis)
    record.setdefault("status", "open")
    record.setdefault("last_reviewed", record["open_date"])
    record.setdefault("cash_qty", None)
    _validate_thesis(record)

    path = _thesis_path(record["ticker"], record["open_date"], memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(record, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    _maybe_commit(
        f"thesis: open {record['ticker']} ({record['horizon_tag']})",
        memory_dir=memory_dir,
        commit=commit,
        runner=runner,
    )
    return path


def read_thesis(
    ticker: str,
    open_date: str,
    *,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
) -> dict[str, Any]:
    path = _thesis_path(ticker, open_date, Path(memory_dir))
    if not path.exists():
        raise FileNotFoundError(f"no thesis for {ticker} opened {open_date}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def list_open_theses(
    *,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
) -> list[dict[str, Any]]:
    """All open theses. ``EXAMPLE_*`` template files are skipped — they are
    committed to the public repo as documentation, not real positions."""
    theses_dir = _theses_dir(Path(memory_dir))
    if not theses_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(theses_dir.glob("*.y*ml")):
        if path.name.startswith("EXAMPLE_"):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data and data.get("status", "open") == "open":
            out.append(data)
    return out


def list_all_theses(
    *,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
) -> list[dict[str, Any]]:
    """Every thesis, open AND closed — the scorecard's input (closed theses carry
    the realized outcomes; open ones the marks-to-market). ``EXAMPLE_*`` templates
    are skipped, same as ``list_open_theses``."""
    theses_dir = _theses_dir(Path(memory_dir))
    if not theses_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(theses_dir.glob("*.y*ml")):
        if path.name.startswith("EXAMPLE_"):
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data:
            out.append(data)
    return out


def close_thesis(
    ticker: str,
    open_date: str,
    *,
    close_date: str,
    exit_price: float,
    realized_pnl: float,
    alpha_vs_spy: float | None = None,
    lesson: str | None = None,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
    commit: bool = False,
    runner: GitRunner | None = None,
) -> dict[str, Any]:
    """Close a thesis in place, adding the §F realized-outcome fields. The
    baseline_snapshot and the rest of the open record are preserved untouched."""
    memory_dir = Path(memory_dir)
    record = read_thesis(ticker, open_date, memory_dir=memory_dir)
    record.update(
        status="closed",
        close_date=close_date,
        exit_price=exit_price,
        realized_pnl=realized_pnl,
        alpha_vs_spy=alpha_vs_spy,
        lesson=lesson,
    )
    path = _thesis_path(ticker, open_date, memory_dir)
    path.write_text(
        yaml.safe_dump(record, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    _maybe_commit(
        f"thesis: close {ticker} (pnl {realized_pnl:g})",
        memory_dir=memory_dir,
        commit=commit,
        runner=runner,
    )
    return record


def update_last_reviewed(
    ticker: str,
    open_date: str,
    *,
    reviewed_at: str | None = None,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
    commit: bool = False,
    runner: GitRunner | None = None,
) -> dict[str, Any]:
    """Stamp a thesis with the last time the thesis-check ran against it."""
    memory_dir = Path(memory_dir)
    record = read_thesis(ticker, open_date, memory_dir=memory_dir)
    record["last_reviewed"] = reviewed_at or _now_iso()
    path = _thesis_path(ticker, open_date, memory_dir)
    path.write_text(
        yaml.safe_dump(record, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    _maybe_commit(
        f"thesis: review {ticker}",
        memory_dir=memory_dir,
        commit=commit,
        runner=runner,
    )
    return record


# ── Decision log ─────────────────────────────────────────────────────────────


def append_decision(
    entry: dict[str, Any],
    *,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
    commit: bool = False,
    runner: GitRunner | None = None,
) -> dict[str, Any]:
    """Append one auditable decision to ``decision_log.jsonl`` (append-only).

    A ``timestamp`` is added if absent. The autonomy ceilings read the running
    daily spend/trade-count from this log, so it is also the state that makes
    the §B cumulative ceiling survive restarts and loops.
    """
    memory_dir = Path(memory_dir)
    record = dict(entry)
    record.setdefault("timestamp", _now_iso())
    _append_jsonl(Path(memory_dir) / DECISION_LOG_NAME, record)
    _maybe_commit(
        f"decision: {record.get('intent', 'log')}",
        memory_dir=memory_dir,
        commit=commit,
        runner=runner,
    )
    return record


def read_decision_log(
    *,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
) -> list[dict[str, Any]]:
    return _read_jsonl(Path(memory_dir) / DECISION_LOG_NAME)


def build_own_sent_orders(
    ticker: str | None = None,
    *,
    now: datetime | str | None = None,
    within_seconds: int | None = None,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
) -> dict[str, Any]:
    """Emit ``reconcile``-ready ``own_sent_orders`` rows from the decision log.

    ``reconcile`` needs each order shaped as ``{ticker, side, value|qty,
    timestamp, status}``, but the journaled ``executed_orders`` may omit the
    per-order ``timestamp``/``status``. This reconstructs the shape WITHOUT the
    LLM hand-threading it: per order, ``timestamp`` falls back to the decision
    entry's ``timestamp`` and ``status`` to ``"filled"`` (a journaled order is a
    recorded fill). Optionally filter to ``ticker`` and to orders within
    ``within_seconds`` of ``now``. Pipe ``own_sent_orders`` straight into
    ``reconcile`` (which re-applies its own lag window to decide double-buy risk).
    """
    moment = _parse_dt(now) or datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for entry in read_decision_log(memory_dir=memory_dir):
        decision_ts = entry.get("timestamp")
        for order in entry.get("executed_orders", []) or []:
            if ticker is not None and order.get("ticker") != ticker:
                continue
            ts = order.get("timestamp") or decision_ts
            if within_seconds is not None and ts is not None:
                parsed = _parse_dt(ts)
                if parsed is not None and (moment - parsed).total_seconds() > within_seconds:
                    continue
            rows.append(
                {
                    "ticker": order.get("ticker"),
                    "side": order.get("side"),
                    "value": order.get("value"),
                    "qty": order.get("qty"),
                    "timestamp": ts,
                    "status": order.get("status") or "filled",
                    "order_id": order.get("order_id"),
                }
            )
    return {"own_sent_orders": rows, "count": len(rows)}


# ── Autonomy day-state: the FIXED baseline + code-sourced running totals ─────
# This is what closes the §B seam. Instead of the LLM re-summing the decision
# log by hand each round (and forgetting), the day baseline is captured once
# when autonomy is armed and the running spend/trade totals are aggregated from
# the decision log by code. The composed gate lives in ``vizier.autonomy``.


def _autonomy_state_path(memory_dir: Path) -> Path:
    return Path(memory_dir) / AUTONOMY_STATE_NAME


def _read_autonomy_state(memory_dir: Path) -> dict[str, Any]:
    path = _autonomy_state_path(memory_dir)
    if not path.exists():
        return {"active": None, "history": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_autonomy_state(memory_dir: Path, state: dict[str, Any]) -> None:
    path = _autonomy_state_path(memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")


def _aggregate_executions(
    memory_dir: str | Path, since: datetime
) -> tuple[float, int, list[dict[str, Any]]]:
    """Sum executed orders from the decision log since ``since``.

    Returns ``(spent, trades, executed_orders)`` where ``spent`` = sum of
    BUY-side ``value`` (sells/closes reduce risk, never consume the spend
    ceiling) and ``trades`` = count of ANY executed order. Shared by the daily
    and per-run aggregators so both windows count identically.
    """
    spent = 0.0
    trades = 0
    executed: list[dict[str, Any]] = []
    for entry in read_decision_log(memory_dir=memory_dir):
        entry_time = _parse_dt(entry.get("timestamp"))
        if entry_time is None or entry_time < since:
            continue
        for order in entry.get("executed_orders", []) or []:
            executed.append(order)
            trades += 1
            if str(order.get("side", "")).upper() in BUY_SIDES:
                spent += float(order.get("value") or 0.0)
    return spent, trades, executed


def arm_autonomy(
    nav: float,
    *,
    now: datetime | str | None = None,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
    commit: bool = False,
    runner: GitRunner | None = None,
) -> dict[str, Any]:
    """Turn autonomy on and FIX the day baseline at the current NAV.

    This is the user action that arms autonomy; it is also the manual re-arm
    point after a drawdown kill (spec, §B: "re-arme manual"). The captured
    ``baseline_nav`` and ``window_start`` anchor the cumulative ceiling and the
    drawdown kill for the next 24h — a shrinking account never moves the anchor.

    **Refuses an early re-arm.** If an armed window is still active (unexpired),
    this raises ``AutonomyAlreadyArmedError`` and does NOT touch the state —
    re-arming mid-window would reset the day baseline and wipe the cumulative
    spend (the §B drain vector). A legitimate re-arm runs only after an explicit
    ``disarm_autonomy`` (so ``active`` is None) or after the window expires.
    """
    if nav <= 0:
        raise ValueError("nav must be positive")
    memory_dir = Path(memory_dir)
    moment = _parse_dt(now) or datetime.now(UTC)
    iso = moment.isoformat()

    # Code-enforced re-arm guard: an active, unexpired window must be explicitly
    # disarmed (or allowed to expire) before a new arm. Checked BEFORE any write.
    existing = current_day_baseline(now=moment, memory_dir=memory_dir)
    if existing is not None:
        raise AutonomyAlreadyArmedError(
            "autonomy already armed; window active until "
            f"{existing['expires_at']}; disarm explicitly (e.g. after a kill) or "
            "wait for expiry to re-arm - re-arming mid-window would reset the "
            "daily ceiling and allow draining"
        )

    state = _read_autonomy_state(memory_dir)
    active = {"baseline_nav": float(nav), "window_start": iso, "armed_at": iso}
    state["active"] = active
    state["run_start"] = None  # a fresh arm has no run until begin_run is called
    # A fresh arm is the explicit manual re-arm after a drawdown kill: it clears
    # the persisted kill latch so the new armed window starts clean. (Re-arming
    # mid-window is already refused above, so this only runs for a legitimate
    # re-arm — after a disarm or window expiry.)
    state["killed"] = False
    state["killed_reason"] = None
    state["killed_at"] = None
    state.setdefault("history", []).append(
        {"event": "arm", "timestamp": iso, "baseline_nav": float(nav)}
    )
    _write_autonomy_state(memory_dir, state)
    _maybe_commit(
        f"autonomy: arm baseline {nav:g}",
        memory_dir=memory_dir,
        commit=commit,
        runner=runner,
    )
    return active


def disarm_autonomy(
    *,
    now: datetime | str | None = None,
    reason: str | None = None,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
    commit: bool = False,
    runner: GitRunner | None = None,
) -> dict[str, Any]:
    """Turn autonomy off. After a drawdown kill the skill calls this; autonomy
    then stays off until the user explicitly re-arms (``arm_autonomy``)."""
    memory_dir = Path(memory_dir)
    moment = _parse_dt(now) or datetime.now(UTC)
    iso = moment.isoformat()

    state = _read_autonomy_state(memory_dir)
    state["active"] = None
    state["run_start"] = None
    # Disarm also clears the kill latch: the post-kill path disarms, and the
    # only way back to armed is a fresh arm (which re-checks the latch anyway).
    state["killed"] = False
    state["killed_reason"] = None
    state["killed_at"] = None
    state.setdefault("history", []).append(
        {"event": "disarm", "timestamp": iso, "reason": reason}
    )
    _write_autonomy_state(memory_dir, state)
    _maybe_commit(
        "autonomy: disarm",
        memory_dir=memory_dir,
        commit=commit,
        runner=runner,
    )
    return {"disarmed": True, "at": iso, "reason": reason}


def begin_run(
    *,
    now: datetime | str | None = None,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
    commit: bool = False,
    runner: GitRunner | None = None,
) -> dict[str, Any]:
    """Mark the start of one autonomous run/round.

    Per-run spend/trade totals are aggregated from the decision log since THIS
    marker, so the skill calls ``begin_run`` at the start of every autonomous
    batch. Without it the per-run gate cannot enforce, and the gate refuses
    (begin_run, or no per-run safety) — the same "journal or the guarantee goes
    hollow" discipline as the daily ceiling.
    """
    memory_dir = Path(memory_dir)
    moment = _parse_dt(now) or datetime.now(UTC)
    iso = moment.isoformat()

    state = _read_autonomy_state(memory_dir)
    state["run_start"] = iso
    state.setdefault("history", []).append({"event": "begin_run", "timestamp": iso})
    _write_autonomy_state(memory_dir, state)
    _maybe_commit(
        "autonomy: begin run",
        memory_dir=memory_dir,
        commit=commit,
        runner=runner,
    )
    return {"run_start": iso}


def latch_drawdown_kill(
    reason: str,
    *,
    now: datetime | str | None = None,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
    commit: bool = False,
    runner: GitRunner | None = None,
) -> dict[str, Any]:
    """Persist the drawdown-kill latch so the kill SELF-LATCHES in code.

    Recomputing the kill from ``current_nav`` alone is not enough: if NAV dips to
    the kill threshold (gate blocks) but the skill never disarms, a later NAV
    recovery would silently re-allow autonomy. Once latched here, the composed
    gate hard-blocks regardless of recovered NAV until an explicit disarm or a
    fresh ``arm_autonomy`` clears it. Idempotent: a second call is a no-op.
    """
    memory_dir = Path(memory_dir)
    moment = _parse_dt(now) or datetime.now(UTC)
    iso = moment.isoformat()

    state = _read_autonomy_state(memory_dir)
    if state.get("killed"):
        return {
            "killed": True,
            "killed_reason": state.get("killed_reason"),
            "killed_at": state.get("killed_at"),
        }
    state["killed"] = True
    state["killed_reason"] = reason
    state["killed_at"] = iso
    state.setdefault("history", []).append(
        {"event": "drawdown_kill", "timestamp": iso, "reason": reason}
    )
    _write_autonomy_state(memory_dir, state)
    _maybe_commit(
        "autonomy: drawdown kill latched",
        memory_dir=memory_dir,
        commit=commit,
        runner=runner,
    )
    return {"killed": True, "killed_reason": reason, "killed_at": iso}


def drawdown_kill_latch(
    *,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
) -> dict[str, Any]:
    """The persisted drawdown-kill latch (durable across the armed window)."""
    state = _read_autonomy_state(Path(memory_dir))
    return {
        "killed": bool(state.get("killed")),
        "killed_reason": state.get("killed_reason"),
        "killed_at": state.get("killed_at"),
    }


def current_day_baseline(
    *,
    now: datetime | str | None = None,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
) -> dict[str, Any] | None:
    """The active autonomy baseline, or None if not armed or the 24h window has
    expired. A None here MUST be treated by the caller as 'autonomy not allowed'."""
    moment = _parse_dt(now) or datetime.now(UTC)
    state = _read_autonomy_state(Path(memory_dir))
    active = state.get("active")
    if not active:
        return None

    window_start = _parse_dt(active["window_start"])
    if window_start is None:
        return None
    if (moment - window_start).total_seconds() > AUTONOMY_WINDOW_SECONDS:
        return None  # window expired -> must re-arm

    expires_at = window_start + timedelta(seconds=AUTONOMY_WINDOW_SECONDS)
    return {
        "baseline_nav": active["baseline_nav"],
        "window_start": active["window_start"],
        "armed_at": active.get("armed_at"),
        "expires_at": expires_at.isoformat(),
    }


def autonomy_day_state(
    *,
    now: datetime | str | None = None,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
) -> dict[str, Any]:
    """Code-sourced day-state: fixed baseline + running spend/trade totals.

    Aggregates EXECUTED orders from the decision log since the active window
    started. ``spent_today`` = sum of BUY-side ``value`` (sells/closes never
    consume the spend ceiling). ``trades_today`` = count of ANY executed order.
    Everything within an armed window counts regardless of mode — the window
    only exists while armed, so pre-arm confirmation trades fall outside it.
    """
    moment = _parse_dt(now) or datetime.now(UTC)
    base = current_day_baseline(now=moment, memory_dir=memory_dir)
    if base is None:
        return {
            "armed": False,
            "reason": "autonomy not armed or 24h window expired",
            "baseline_nav": None,
            "window_start": None,
            "spent_today": 0.0,
            "trades_today": 0,
            "executed_orders": [],
        }

    window_start = _parse_dt(base["window_start"])
    spent_today, trades_today, executed = _aggregate_executions(memory_dir, window_start)

    return {
        "armed": True,
        "baseline_nav": base["baseline_nav"],
        "window_start": base["window_start"],
        "expires_at": base["expires_at"],
        "spent_today": spent_today,
        "trades_today": trades_today,
        "executed_orders": executed,
    }


def autonomy_run_state(
    *,
    now: datetime | str | None = None,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
) -> dict[str, Any]:
    """Code-sourced per-run state: spend/trade totals since the run marker.

    A run is active only while armed AND a ``begin_run`` marker was set within
    the current armed window (a marker from a prior arming is treated as stale).
    Per-run totals reset on each ``begin_run`` while the daily totals keep
    accumulating from ``window_start`` — the two anchors coexist.
    """
    moment = _parse_dt(now) or datetime.now(UTC)
    base = current_day_baseline(now=moment, memory_dir=memory_dir)
    run_start = _parse_dt(_read_autonomy_state(Path(memory_dir)).get("run_start"))

    inactive = {
        "run_active": False,
        "run_start": None,
        "spent_this_run": 0.0,
        "trades_this_run": 0,
        "executed_orders": [],
    }
    if base is None or run_start is None:
        return inactive
    if run_start < _parse_dt(base["window_start"]):
        return inactive  # stale marker from a prior arming

    spent_this_run, trades_this_run, executed = _aggregate_executions(memory_dir, run_start)
    return {
        "run_active": True,
        "run_start": run_start.isoformat(),
        "spent_this_run": spent_this_run,
        "trades_this_run": trades_this_run,
        "executed_orders": executed,
    }


# ── NAV snapshots + drawdown ─────────────────────────────────────────────────


def record_nav_snapshot(
    net_liquidation: float,
    *,
    timestamp: str | None = None,
    available_funds: float | None = None,
    currency: str = "USD",
    venue: str | None = None,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
    commit: bool = False,
    runner: GitRunner | None = None,
) -> dict[str, Any]:
    """Append a NAV snapshot. Neither MCP keeps a NAV history (spec, §A), so the
    circuit-breaker's monthly-drawdown leg depends entirely on this series."""
    memory_dir = Path(memory_dir)
    record = {
        "timestamp": timestamp or _now_iso(),
        "net_liquidation": net_liquidation,
        "available_funds": available_funds,
        "currency": currency,
        "venue": venue,
    }
    _append_jsonl(Path(memory_dir) / NAV_SNAPSHOTS_NAME, record)
    _maybe_commit(
        f"nav: snapshot {net_liquidation:g} {currency}",
        memory_dir=memory_dir,
        commit=commit,
        runner=runner,
    )
    return record


def compute_drawdown(
    *,
    window_days: int | None = None,
    venue: str | None = None,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
) -> dict[str, Any]:
    """Peak-to-trough drawdown over the NAV series.

    Returns both the worst drawdown in the window (``max_drawdown_pct``) and the
    drawdown from the running peak to the latest reading (``current_drawdown_pct``,
    which is what the monthly-drawdown breaker leg consumes). ``window_days``
    keeps only snapshots within that many days of the most recent one.

    NAV is **per venue** (SKILL.md: never mix an IBKR NAV with a crypto-exchange
    NAV under one denominator). ``venue`` selects one venue's snapshots; when the
    stored series spans MORE than one venue and no ``venue`` is given, this
    raises instead of interleaving them — an interleaved series (e.g. $8 IBKR,
    $1000 crypto, $8 IBKR…) reads as a phantom ~99% drawdown and would trip the
    breaker on garbage. A single-venue series needs no filter.
    """
    snapshots = _read_jsonl(Path(memory_dir) / NAV_SNAPSHOTS_NAME)
    if venue is not None:
        snapshots = [s for s in snapshots if s.get("venue") == venue]
    else:
        venues_present = {s.get("venue") for s in snapshots}
        if len(venues_present) > 1:
            names = ", ".join(sorted(str(v) for v in venues_present))
            raise ValueError(
                f"NAV snapshots span multiple venues ({names}); pass 'venue' to "
                "compute a per-venue drawdown - mixing venues in one series "
                "fabricates a drawdown that never happened"
            )
    series = [
        (datetime.fromisoformat(s["timestamp"]), float(s["net_liquidation"]))
        for s in snapshots
        if s.get("net_liquidation") is not None
    ]
    series.sort(key=lambda pair: pair[0])

    if window_days is not None and series:
        cutoff = series[-1][0].timestamp() - window_days * 86400
        series = [pair for pair in series if pair[0].timestamp() >= cutoff]

    if len(series) < 2:
        return {
            "max_drawdown_pct": 0.0,
            "current_drawdown_pct": 0.0,
            "peak": series[-1][1] if series else None,
            "trough": series[-1][1] if series else None,
            "samples": len(series),
        }

    peak = series[0][1]
    trough = series[0][1]
    max_drawdown = 0.0
    for _, nav in series:
        peak = max(peak, nav)
        trough = min(trough, nav)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - nav) / peak * 100.0)

    running_peak = max(nav for _, nav in series)
    latest = series[-1][1]
    current_drawdown = (running_peak - latest) / running_peak * 100.0 if running_peak > 0 else 0.0

    return {
        "max_drawdown_pct": max_drawdown,
        "current_drawdown_pct": max(0.0, current_drawdown),
        "peak": running_peak,
        "trough": trough,
        "samples": len(series),
    }


# ── Tranche accounting by horizon tag (spec, §D) ─────────────────────────────


def tranche_balances(
    ticker: str,
    *,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
) -> dict[str, float]:
    """Open share quantity for ``ticker`` grouped by horizon tag.

    Shares are fungible at the broker, so without this the skill cannot tell a
    `tactical` lot from a `core` lot — a short-term sell would silently eat the
    core. This reconstructs the per-tag balances from open theses.
    """
    balances = {tag: 0.0 for tag in HORIZON_TAGS}
    for thesis in list_open_theses(memory_dir=memory_dir):
        if thesis.get("ticker") != ticker:
            continue
        tag = thesis.get("horizon_tag")
        qty = thesis.get("qty")
        if tag in balances and qty is not None:
            balances[tag] += float(qty)
    return balances


def check_tranche_sell(
    ticker: str,
    tag: str,
    qty: float,
    *,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
) -> dict[str, Any]:
    """May a sell of ``qty`` shares draw down the ``tag`` tranche of ``ticker``?

    A tactical sell can only reduce the tactical tranche; if it would exceed
    what that tranche holds it must be blocked and the contradiction surfaced to
    the user, not allowed to consume the core (spec, §D).
    """
    if tag not in HORIZON_TAGS:
        raise ValueError(f"tag must be one of {HORIZON_TAGS}, got {tag!r}")
    balances = tranche_balances(ticker, memory_dir=memory_dir)
    available = balances.get(tag, 0.0)
    allowed = qty <= available + 1e-9
    return {
        "allowed": allowed,
        "ticker": ticker,
        "tag": tag,
        "requested": qty,
        "available_in_tranche": available,
        "balances": balances,
        "message": (
            f"sell {qty:g} {ticker} ok against {tag} tranche ({available:g} available)"
            if allowed
            else (
                f"sell {qty:g} {ticker} would exceed the {tag} tranche "
                f"({available:g} available) - would eat another tranche; surface to user"
            )
        ),
    }


# ── Private git repo for the memory ──────────────────────────────────────────


def commit_memory(
    message: str,
    *,
    memory_dir: str | Path = DEFAULT_MEMORY_DIR,
    runner: GitRunner | None = None,
    init: bool = True,
    push: bool = True,
) -> None:
    """Commit the memory directory to its OWN private git repo — and push it.

    The memory is a separate, private repository from the public skill repo:
    real theses, the decision log and NAV snapshots must never reach GitHub
    publicly, so they get their own history here. It is created on first use
    (``git init``). Once a PRIVATE remote is attached (one-time)::

        cd memory && git remote add origin <private-repo-url> && git push -u origin HEAD

    every commit is also pushed — the memory is the project's track record (the
    scorecard's raw material), and a disk failure must not erase it. The push is
    BEST-EFFORT by design: it runs after the local commit, a network failure
    only skips the backup (never breaks a trading flow), and with no remote
    attached it is a no-op.

    Committing is injectable/optional everywhere (writers default ``commit=False``)
    so tests never shell out to git; pass a fake ``runner`` to observe calls.
    """
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    run = runner or _default_git_runner
    if init and not (memory_dir / ".git").exists():
        run(["init"], memory_dir)
    run(["add", "-A"], memory_dir)
    run(["commit", "-m", message], memory_dir)
    if push:
        remotes = run(["remote"], memory_dir)
        names = (getattr(remotes, "stdout", "") or "").split()
        if names:
            run(["push", names[0], "HEAD"], memory_dir)
