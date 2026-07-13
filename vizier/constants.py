"""Shared named constants for the deterministic core.

Kept in one place so the money-sensitive math never carries magic numbers and
so the few cross-module values have a single source of truth.
"""

from __future__ import annotations

# Conviction is a 1-5 rubric (spec, §F). Sizing is linear in conviction/5.
MIN_CONVICTION = 1
MAX_CONVICTION = 5

# Config limits are whole-number percentages of NAV (25 means 25%). Divide by
# this to get a fraction.
PERCENT = 100.0

# Broker position state lags the truth by ~30-45s (spec, §B). The skill must
# reconcile a fresh trade against its OWN sent-order log within this window,
# never against lagged positions alone. We assume the hard end of the range as
# the cross-venue default (conservative: it over-flags rather than under-flags).
POSITION_LAG_SECONDS = 45

# Crypto settles faster than IBKR; the crypto `close_position` cooldown is ~30s
# vs IBKR's ~45s. When a reconcile is told its venue is crypto and no explicit
# window is passed, it uses this shorter window. (45s is still safe on crypto —
# it only widens the double-buy guard — so this is a refinement, not a fix.)
CRYPTO_POSITION_LAG_SECONDS = 30

# Autonomy day window (spec, §F: "<=50% do NAV/dia (janela 24h)"). The cumulative
# ceiling is anchored to a FIXED baseline captured when autonomy is armed, and
# that baseline (plus its running spend/trade totals) is valid for this many
# seconds. After it expires, autonomy must be re-armed.
AUTONOMY_WINDOW_SECONDS = 24 * 60 * 60  # 24h

# Float comparison tolerance for percent-of-NAV limit checks, so that a value
# sitting exactly on a limit (e.g. 25.0% against a 25% cap) is not rejected by
# floating-point dust.
LIMIT_EPSILON = 1e-9

# Quantity comparison tolerance (shares / base units). Used when an exit is
# compared against the held position: the held quantity is a HARD ceiling, so
# this only absorbs float dust (a fractional share is ~1e-4, orders of magnitude
# above this) and never lets a real oversize slip through.
QTY_EPSILON = 1e-9

# ── Unit-confusion guard (the US$-quantity bug) ──────────────────────────────
# A "cash quantity" order is sized in DOLLARS (`buy(symbol, cash_amount=2)`), and
# a real IBKR fill of $2 of AAPL @ $317.25 acquired 0.0063 shares. Any number
# whose unit is implicit is a hazard here, so the core cross-checks a recorded
# `qty` (shares) against `cash_qty / entry_price` (the shares those dollars could
# possibly have bought).
#
# Tolerance for "qty agrees with cash_qty/entry_price": generous enough to absorb
# commission, slippage between the quote and the fill, and rounding — but far
# tighter than the order-of-magnitude error a dollars-as-shares mix-up produces.
QTY_CASH_CONSISTENCY_TOLERANCE = 0.25  # 25%

# How far the entry price must sit from 1.0 for the dollars-as-shares signature
# (`qty == cash_qty`) to be conclusive. At a price of exactly $1 the two units
# coincide and the number is ambiguous but harmless; away from 1.0 it is a bug.
QTY_CASH_UNIT_PRICE_RATIO = 1.5
