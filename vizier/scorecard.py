"""Deterministic performance scorecard — does the brain have edge? Measured, not vibed.

Pure functions over the thesis store: no I/O, no network, no clock defaults that
matter. The SKILL fetches the live inputs (current prices for open theses and a
benchmark price history per venue, both from Scout) and passes them in; this module
does the arithmetic the LLM must never do by hand (Rule #2) — per-thesis P&L,
benchmark-relative alpha, hit rate, win/loss profile.

Honesty rules (the same ones the rest of the core follows):
  * a thesis that can't be scored (missing qty/entry price/current price) is SKIPPED
    and NAMED in ``skipped`` — never silently dropped, never guessed;
  * alpha is null when the benchmark series doesn't cover the thesis window, with the
    reason attached — a fabricated benchmark is worse than none;
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

# Fraction of cost basis → percent, and the tags the aggregates group by.
_PERCENT = 100.0
_HORIZON_TAGS = ("core", "tactical")


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


def _benchmark_price_at(series: list[tuple[date, float]], target: date) -> float | None:
    """Last close at-or-before ``target`` (series pre-sorted). None when the series
    starts after the target — an uncovered window must not fake a benchmark."""
    index = bisect_right(series, (target, float("inf")))
    if index == 0:
        return None
    return series[index - 1][1]


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


def _score_thesis(
    thesis: dict[str, Any],
    prices: dict[str, Any],
    benchmarks: dict[str, tuple[str | None, list[tuple[date, float]]]],
    as_of: date,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Score one thesis → (row, skipped). Exactly one of the pair is non-None."""
    ticker = thesis.get("ticker")
    open_date = _to_date(thesis.get("open_date"))
    entry_price = thesis.get("entry_price")
    qty = thesis.get("qty")

    def skip(reason: str) -> tuple[None, dict[str, Any]]:
        return None, {"ticker": ticker, "open_date": thesis.get("open_date"), "reason": reason}

    if not ticker or open_date is None:
        return skip("missing ticker/open_date")
    if entry_price is None or float(entry_price) <= 0 or qty is None or float(qty) <= 0:
        return skip("missing/non-positive entry_price or qty - cannot compute a return")

    entry = float(entry_price)
    quantity = float(qty)
    invested = entry * quantity
    status = thesis.get("status", "open")

    if status == "closed":
        end_date = _to_date(thesis.get("close_date")) or as_of
        exit_price = thesis.get("exit_price")
        if exit_price is None or float(exit_price) <= 0:
            return skip("closed thesis without a usable exit_price")
        end_price = float(exit_price)
        # The recorded realized_pnl is ground truth when present (it may include
        # fees the price diff can't see); the price diff is the fallback.
        recorded = thesis.get("realized_pnl")
        pnl = float(recorded) if recorded is not None else (end_price - entry) * quantity
    else:
        end_date = as_of
        current = prices.get(ticker)
        if current is None or float(current) <= 0:
            return skip("open thesis with no current price passed in 'prices'")
        end_price = float(current)
        pnl = (end_price - entry) * quantity

    return_pct = (end_price / entry - 1.0) * _PERCENT

    venue = _venue(ticker)
    bench_symbol, bench_series = benchmarks.get(venue, (None, []))
    benchmark_return_pct: float | None = None
    alpha_pct: float | None = None
    benchmark_note: str | None = None
    if not bench_series:
        benchmark_note = f"no benchmark series passed for venue '{venue}'"
    else:
        bench_start = _benchmark_price_at(bench_series, open_date)
        bench_end = _benchmark_price_at(bench_series, end_date)
        if bench_start is None or bench_end is None:
            benchmark_note = (
                f"benchmark series does not cover the thesis window "
                f"({open_date.isoformat()} → {end_date.isoformat()})"
            )
        else:
            benchmark_return_pct = (bench_end / bench_start - 1.0) * _PERCENT
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
            "invested": invested,
            "pnl": pnl,
            "return_pct": return_pct,
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
        "realized_pnl": sum(r["pnl"] for r in closed) if closed else None,
        "unrealized_pnl": sum(r["pnl"] for r in open_rows) if open_rows else None,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "win_loss_ratio": (
            abs(avg_win / avg_loss) if avg_win is not None and avg_loss else None
        ),
        "avg_alpha_pct": _mean(alphas),
        "avg_days_held_closed": _mean([float(r["days_held"]) for r in closed]),
        "open_invested": sum(r["invested"] for r in open_rows) if open_rows else None,
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
            "Closed P&L uses the recorded realized_pnl when present (may include fees); "
            "open P&L is a mark-to-market on the passed prices and includes no fees. "
            "Alpha is thesis return minus the same-window benchmark return."
        ),
    }
