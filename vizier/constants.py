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
