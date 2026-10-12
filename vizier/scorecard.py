"""Deterministic performance scorecard — does the brain have edge? Measured, not vibed.

Pure functions over the thesis store: no I/O, no network, no clock defaults that
matter. The SKILL fetches the live inputs (current prices for open theses and a
benchmark price history per venue, both from Scout) and passes them in; this module
does the arithmetic the LLM must never do by hand (Rule #2) — per-thesis P&L,
benchmark-relative alpha, hit rate, win/loss profile.

Honesty rules (the same ones the rest of the core follows):
  * a thesis that can't be scored (missing qty/entry price/current price) is SKIPPED
    and NAMED in ``skipped`` — never silently dropped, never guessed;
  * P&L, return and alpha are NET and consistent: ``return_pct = pnl / invested`` and
    ``alpha_pct = return_pct - benchmark return over the same window`` — never a gross
    price move next to a net P&L;
  * alpha is null when the benchmark series doesn't cover the thesis window at BOTH
    ends, with the reason attached — a fabricated benchmark is worse than none;
  * aggregates over an empty set are null, not zero — "no closed theses" is not
    "0% hit rate".

Venue is derived from the canonical symbol form (SKILL.md memory discipline): crypto
is always ``BASE/QUOTE`` (``BTC/USDT``), so a ``/`` in the ticker means crypto and
anything else is ibkr. Returns are simple period returns — no annualization (a 3-day
tactical and a 6-month core annualize into nonsense; ``days_held`` is reported so the
reader can weigh them).
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date
from typing import Any

from .risk import check_fill_units

# Fraction of cost basis → percent, and the tags the aggregates group by.
_PERCENT = 100.0
_HORIZON_TAGS = ("core", "tactical")

# A benchmark close used for a window endpoint may sit at most this many days
# before it (weekends/holidays make a few days of slack normal; more than that
# means the series was truncated or has a hole, and the windows differ).
_BENCHMARK_MAX_GAP_DAYS = 5

# Relative tolerance when checking that opened = trimmed + remaining (float dust).
_QTY_RECONCILE_TOLERANCE = 1e-6


def _to_date(value: Any) -> date | None:
    """Lenient ISO date: accepts 'YYYY-MM-DD' or a full ISO timestamp (keeps the day)."""
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _venue(ticker: str) -> str:
    return "crypto" if "/" in ticker else "ibkr"


def _benchmark_bar_at(
    series: list[tuple[date, float]], target: date
) -> tuple[date, float] | None:
    """Last (day, close) at-or-before ``target`` (series pre-sorted). None when the
    series starts after the target — an uncovered window must not fake a benchmark."""
    index = bisect_right(series, (target, float("inf")))
    if index == 0:
        return None
    return series[index - 1]


def _parse_benchmark(raw: dict[str, Any] | None) -> tuple[str | None, list[tuple[date, float]]]:
    """Normalize one venue's benchmark input ``{"symbol", "series": [{date, close}]}``
    into a sorted (date, close) list, dropping unparseable rows."""
    if not raw:
        return None, []
    series: list[tuple[date, float]] = []
    for row in raw.get("series", []) or []:
        day = _to_date(row.get("date"))
        close = row.get("close")
        if day is not None and close is not None and float(close) > 0:
            series.append((day, float(close)))
    series.sort(key=lambda pair: pair[0])
    return raw.get("symbol"), series


def _benchmark_return(
    series: list[tuple[date, float]], start: date, end: date
) -> tuple[float | None, str | None]:
    """Benchmark return over EXACTLY the thesis window, or (None, reason).

    Each endpoint must have a close at-or-before it and no more than
    ``_BENCHMARK_MAX_GAP_DAYS`` older: a series that is truncated (or has a hole)
    at either end would silently backfill a stale close and compare unequal
    windows — then there is no honest alpha, only a reason.
    """
    if not series:
        return None, "no benchmark series passed"
    start_bar = _benchmark_bar_at(series, start)
    end_bar = _benchmark_bar_at(series, end)
    if start_bar is None or end_bar is None:
        return None, (
            f"benchmark series does not cover the thesis window "
            f"({start.isoformat()} → {end.isoformat()})"
        )
    for label, target, (bar_day, _) in (("start", start, start_bar), ("end", end, end_bar)):
        gap_days = (target - bar_day).days
        if gap_days > _BENCHMARK_MAX_GAP_DAYS:
            return None, (
                f"benchmark has no close within {_BENCHMARK_MAX_GAP_DAYS} days of the "
                f"window {label} ({bar_day.isoformat()} vs {target.isoformat()}, "
                f"{gap_days} days); alpha would compare unequal windows - not computed"
            )
    return (end_bar[1] / start_bar[1] - 1.0) * _PERCENT, None


def _realized_from_partial_exits(
    thesis: dict[str, Any], qty_opened: float, qty_remaining: float
) -> tuple[float, str | None]:
    """Net realized P&L of the trims already taken, or (0, reason) when the
    bookkeeping cannot be trusted (an unpriced leg, or quantities that do not add
    up to ``qty_opened``)."""
    partial_exits = thesis.get("partial_exits") or []
    sold = sum(float(event.get("qty") or 0.0) for event in partial_exits)
    if abs(qty_opened - sold - qty_remaining) > _QTY_RECONCILE_TOLERANCE * qty_opened:
        return 0.0, (
            f"lot bookkeeping does not reconcile: opened {qty_opened:g} - trimmed "
            f"{sold:g} != remaining {qty_remaining:g}"
        )
    unpriced = [event for event in partial_exits if event.get("realized_pnl") is None]
    if unpriced:
        return 0.0, (
            f"{len(unpriced)} partial exit(s) recorded without exit_price - their realized "
            "P&L is unknown; close the thesis with its TOTAL realized_pnl to score it"
        )
    return sum(float(event["realized_pnl"]) for event in partial_exits), None


def _score_thesis(
    thesis: dict[str, Any],
    prices: dict[str, Any],
    benchmarks: dict[str, tuple[str | None, list[tuple[date, float]]]],
    as_of: date,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Score one thesis → (row, skipped). Exactly one of the pair is non-None.

    P&L and return are NET and computed from the same number: ``return_pct =
    pnl / invested``, where ``invested`` is the cost of the whole lot as opened
    (``qty_opened``, which survives trims). Alpha subtracts the benchmark over
    the same window from that net return — a gross price move can never turn a
    net loss into positive alpha. ``price_return_pct`` keeps the raw price move
    for reference only.
    """
    ticker = thesis.get("ticker")
    open_date = _to_date(thesis.get("open_date"))
    entry_price = thesis.get("entry_price")
    qty = thesis.get("qty")

    def skip(reason: str) -> tuple[None, dict[str, Any]]:
        return None, {"ticker": ticker, "open_date": thesis.get("open_date"), "reason": reason}

    if not ticker or open_date is None:
        return skip("missing ticker/open_date")
    qty_opened_raw = thesis.get("qty_opened", qty)
    if (
        entry_price is None
        or float(entry_price) <= 0
        or qty is None
        or float(qty) < 0
        or qty_opened_raw is None
        or float(qty_opened_raw) <= 0
    ):
        return skip("missing/non-positive entry_price or qty - cannot compute a return")

    # `qty` MUST be shares/base units. A record written before the unit guard (or
    # hand-edited) could hold DOLLARS there — IBKR reports a cash-quantity order's
    # fill in dollars — and `entry * quantity` would then report a P&L off by the
    # share price. Naming it in `skipped` is honest; a fabricated number is not.
    # Checked on the lot AS OPENED: trims scale qty and cash_qty together.
    units = check_fill_units(
        qty=qty_opened_raw,
        cash_qty=thesis.get("cash_qty_opened", thesis.get("cash_qty")),
        entry_price=entry_price,
    )
    if units["unit_error"] or (units["checked"] and not units["consistent"]):
        return skip(f"qty is not a usable share quantity - {units['reason']}")

    entry = float(entry_price)
    qty_opened = float(qty_opened_raw)
    quantity = float(qty)
    if quantity > qty_opened * (1 + _QTY_RECONCILE_TOLERANCE):
        return skip(f"qty {quantity:g} exceeds qty_opened {qty_opened:g}")
    invested = entry * qty_opened
    status = thesis.get("status", "open")

    if status == "closed":
        end_date = _to_date(thesis.get("close_date")) or as_of
        exit_price = thesis.get("exit_price")
        if exit_price is None or float(exit_price) <= 0:
            return skip("closed thesis without a usable exit_price")
        end_price = float(exit_price)
        # The recorded realized_pnl is the thesis TOTAL net P&L (fees and earlier
        # trims included) — ground truth. Without it, rebuild from the priced
        # trims plus the price diff on what was left (gross of that leg's fees).
        recorded = thesis.get("realized_pnl")
        if recorded is not None:
            pnl = float(recorded)
            pnl_basis = "recorded_realized_pnl"
        else:
            realized, problem = _realized_from_partial_exits(thesis, qty_opened, quantity)
            if problem:
                return skip(problem)
            pnl = realized + (end_price - entry) * quantity
            pnl_basis = "price_diff"
        realized_part = pnl
    else:
        end_date = as_of
        realized_part, problem = _realized_from_partial_exits(thesis, qty_opened, quantity)
        if problem:
            return skip(problem)
        if quantity == 0:
            partial_exits = thesis.get("partial_exits") or []
            if not partial_exits:
                return skip("open thesis with qty 0 and no recorded exits")
            end_price = float(partial_exits[-1]["exit_price"])
        else:
            current = prices.get(ticker)
            if current is None or float(current) <= 0:
                return skip("open thesis with no current price passed in 'prices'")
            end_price = float(current)
        pnl = realized_part + (end_price - entry) * quantity
        pnl_basis = "mark_to_market"

    return_pct = pnl / invested * _PERCENT
    price_return_pct = (end_price / entry - 1.0) * _PERCENT

    venue = _venue(ticker)
    bench_symbol, bench_series = benchmarks.get(venue, (None, []))
    benchmark_return_pct: float | None = None
    alpha_pct: float | None = None
    if not bench_series:
        benchmark_note: str | None = f"no benchmark series passed for venue '{venue}'"
    else:
        benchmark_return_pct, benchmark_note = _benchmark_return(
            bench_series, open_date, end_date
        )
        if benchmark_return_pct is not None:
            alpha_pct = return_pct - benchmark_return_pct

    return (
        {
            "ticker": ticker,
            "venue": venue,
            "status": status,
            "horizon_tag": thesis.get("horizon_tag"),
            "conviction": thesis.get("conviction"),
            "open_date": open_date.isoformat(),
            "end_date": end_date.isoformat(),
            "days_held": (end_date - open_date).days,
            "entry_price": entry,
            "end_price": end_price,
            "qty": quantity,
            "qty_opened": qty_opened,
            "invested": invested,
            "open_cost_basis": entry * quantity if status != "closed" else 0.0,
            "pnl": pnl,
            "realized_pnl": realized_part,
            "pnl_basis": pnl_basis,
            "return_pct": return_pct,
            "price_return_pct": price_return_pct,
            "benchmark_symbol": bench_symbol,
            "benchmark_return_pct": benchmark_return_pct,
            "alpha_pct": alpha_pct,
            "benchmark_note": benchmark_note,
        },
        None,
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a set of scored rows. Closed rows carry the realized verdicts; open
    rows the marks-to-market. Null (not zero) when a statistic has no sample."""
    closed = [r for r in rows if r["status"] == "closed"]
    open_rows = [r for r in rows if r["status"] != "closed"]
    wins = [r["pnl"] for r in closed if r["pnl"] > 0]
    losses = [r["pnl"] for r in closed if r["pnl"] < 0]
    alphas = [r["alpha_pct"] for r in rows if r["alpha_pct"] is not None]
    avg_win = _mean(wins)
    avg_loss = _mean(losses)
    return {
        "closed_count": len(closed),
        "open_count": len(open_rows),
        "hit_rate_pct": (len(wins) / len(closed) * _PERCENT) if closed else None,
        # Realized = closed theses + trims already taken on open ones; unrealized =
        # the mark-to-market on what is still held.
        "realized_pnl": sum(r["realized_pnl"] for r in rows) if rows else None,
        "unrealized_pnl": (
            sum(r["pnl"] - r["realized_pnl"] for r in open_rows) if open_rows else None
        ),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "win_loss_ratio": (
            abs(avg_win / avg_loss) if avg_win is not None and avg_loss else None
        ),
        "avg_alpha_pct": _mean(alphas),
        "avg_days_held_closed": _mean([float(r["days_held"]) for r in closed]),
        "open_invested": sum(r["open_cost_basis"] for r in open_rows) if open_rows else None,
    }


def compute_scorecard(
    theses: list[dict[str, Any]],
    *,
    prices: dict[str, Any] | None = None,
    benchmarks: dict[str, dict[str, Any]] | None = None,
    as_of: str,
    decision_log: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score every thesis and aggregate — overall, by horizon tag and by venue.

    ``theses``: the full store (open + closed; ``memory.list_all_theses``).
    ``prices``: ticker → current price, needed to mark OPEN theses.
    ``benchmarks``: per venue (``ibkr``/``crypto``), ``{"symbol", "series":
    [{"date", "close"}, …]}`` — a plain dump of Scout's ``price_history`` bars.
    ``as_of``: the evaluation date (ISO) — REQUIRED so the result is reproducible
    (no hidden clock). ``decision_log`` adds an activity summary when given.
    """
    evaluation_date = _to_date(as_of)
    if evaluation_date is None:
        raise ValueError("as_of must be an ISO date (YYYY-MM-DD)")

    parsed_benchmarks = {
        venue: _parse_benchmark((benchmarks or {}).get(venue)) for venue in ("ibkr", "crypto")
    }

    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for thesis in theses:
        row, skip = _score_thesis(thesis, prices or {}, parsed_benchmarks, evaluation_date)
        if row is not None:
            rows.append(row)
        else:
            skipped.append(skip)

    by_horizon = {
        tag: _aggregate([r for r in rows if r["horizon_tag"] == tag]) for tag in _HORIZON_TAGS
    }
    by_venue = {
        venue: _aggregate([r for r in rows if r["venue"] == venue])
        for venue in ("ibkr", "crypto")
        if any(r["venue"] == venue for r in rows)
    }

    activity: dict[str, Any] | None = None
    if decision_log is not None:
        executed = [
            order
            for entry in decision_log
            for order in (entry.get("executed_orders") or [])
        ]
        activity = {
            "decisions": len(decision_log),
            "decisions_with_executions": sum(
                1 for entry in decision_log if entry.get("executed_orders")
            ),
            "executed_orders": len(executed),
            "buy_value_total": sum(
                float(order.get("value") or 0.0)
                for order in executed
                if str(order.get("side", "")).upper() == "BUY"
            ),
        }

    return {
        "as_of": evaluation_date.isoformat(),
        "theses": rows,
        "skipped": skipped,
        "overall": _aggregate(rows),
        "by_horizon": by_horizon,
        "by_venue": by_venue,
        "activity": activity,
        "note": (
            "Returns are simple period returns (no annualization; weigh days_held). "
            "return_pct = pnl / invested (cost of the lot as opened): closed P&L is the "
            "recorded TOTAL realized_pnl (fees and trims included); open P&L = priced "
            "trims (net of their fees) + a fee-free mark-to-market of what is still held. "
            "Alpha = that net return minus the benchmark's price return over the same "
            "window (null when the series misses either end); price_return_pct is the "
            "raw price move, for reference only."
        ),
    }
