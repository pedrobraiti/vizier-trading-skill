# Execution mechanics — the §C venue cheatsheet

> **`python -m vizier …` = the core interpreter resolved in SKILL.md** (the skill's bundled `.venv`
> python), never bare system `python`.


Read this before sending any order. The two Valet servers mirror tool **names** but differ in
**capabilities**. Get the venue's quirks right or you mis-size, double-buy, or leave a position
unprotected.

The Valet returns `{"ok", "data"}` like Scout. **`account_type` (`"LIVE"`/`"PAPER"`) is the assertion
field — it is present and identically named on BOTH venues.** It comes from IBKR's real `isPaper` on the
`ibkr` server and from `CRYPTO_TRADING_MODE` on the `crypto` server — **not** the config label; trust the
account, never the label. The raw boolean differs by venue (`isPaper` on IBKR, `is_paper` on crypto), so
**key off `account_type`, never a venue-specific boolean** (asserting `isPaper` on a crypto response reads
a field that isn't there → silently passes, which is exactly how a "shadow" run leaks into LIVE).

**The venues are asymmetric on how trustworthy `account_type` is.** On IBKR it is **broker-verified** — it
derives from the broker's real `isPaper`, so a mislabel is independently caught. On CRYPTO it only
reflects your own **`CRYPTO_TRADING_MODE`** setting; the exchange does not hand back a paper/live truth, so
`account_type` **cannot independently detect a mislabel** there. Real-money protection on crypto therefore
rests on the **`CRYPTO_ALLOW_LIVE`** gate (and `CRYPTO_DRY_RUN`), **not** on `account_type` — treat the
crypto `account_type` as a self-report, and never let it alone authorize a live order.

---

## Pre-flight (both venues, before EVERY order)

1. `session_status` → assert **`account_type`** (`"PAPER"`/`"LIVE"`) matches the mode you think you're
   in. Mismatch = abort the batch and tell the user. (Re-run this immediately before each order — a
   session can drop between a preview and the buy.)
2. `market_status` → tradeable now? (`crypto` is `ALWAYS_OPEN`; IBKR is RTH-only with NYSE holidays.)
3. `portfolio` / `positions` + `account_summary` → read the **real** book. NAV (`net_liquidation`) is the
   denominator for limits; free cash / `available_funds` is what you can actually deploy this round.
   Buying power ≠ NAV (margin) — never size off buying power.
4. `reconcile` your own recently-sent orders ∪ these positions (assume 30-45s lag) before deciding —
   never double a buy that's already in flight. See **"Building the `reconcile` input"** below — feeding
   it the wrong shape makes it flag every ticker forever (or, worse, trains you to ignore it).

---

## IBKR server (`ibkr`) — 19 tools

`session_status, market_status, get_quote, get_quotes, account_summary, positions, portfolio,
preview_order, buy, sell, close_position, stop_order, trailing_stop, bracket_order, order_status,
wait_for_fill, cancel_order, open_orders, trade_history`

**Order flow (mandatory):**
1. `preview_order(symbol, side, ...)` — IBKR `whatif`: estimates commission, margin impact, warnings,
   **without sending**. Read it and reason about cost before committing.
2. `buy(symbol, cash_amount=USD)` → **market-only** (fractional via `cashQty`). For a **LIMIT** you must
   use `buy(symbol, quantity=shares, limit_price=...)` — `cash_amount` cannot be a LIMIT.
   `sell(symbol, quantity, limit_price?)` — **sell is by share quantity only** (no `cashQty`).
   `close_position(symbol)` closes 100% by resolving the exact fractional quantity.
3. After a fill, confirm with `order_status(order_id)` (→ `filled_quantity`, avg price) or
   `wait_for_fill(order_id, timeout_seconds)`.
4. **Protective stop is placed AFTER the fill**, sized by the filled quantity:
   `stop_order(symbol, side, quantity, stop_price, limit_price?)` or, for entry+exits as one,
   `bracket_order(symbol, quantity, take_profit, stop_loss, ...)` (OCO). A US$ market buy only reveals
   its share count after filling, so a `bracket_order` can't be the entry for a dollar buy — buy first,
   read `filled_quantity`, then attach the stop. (`trailing_stop(symbol, side, quantity,
   trail_amount|trail_percent)` for a trailing exit.)
5. `close_position` has a ~45s cooldown + in-flight sentinel. Just after a buy it may report the
   position as not-yet-visible; just after a close, a re-close is refused for ~45s. **Confirm fills via
   `order_status`/`wait_for_fill`, never by re-calling `close_position`** (a naïve retry loop hits the
   guard).

**Stops survive you.** An IBKR `stop_order`/`bracket_order` is a resting order at the broker — it fires
even if this skill is offline.

---

## Crypto server (`crypto`) — 14 tools (the 5 IBKR-only tools are ABSENT)

`session_status, market_status, get_quote, get_quotes, account_summary, portfolio, positions, buy,
sell, close_position, cancel_order, open_orders, order_status, trade_history`

**Absent:** `preview_order`, `stop_order`, `trailing_stop`, `bracket_order`, `wait_for_fill`.
Consequences you MUST respect:

- **No preview.** Estimate cost with `get_quote`/`get_quotes`. You **cannot** pre-validate the exchange
  **notional/amount minimum** — neither Scout nor the crypto Valet exposes ccxt `market['limits']`, and
  there is no `preview_order`. So for a small dollar buy, **send it and treat a below-minimum rejection
  as an expected, clean "too small" refusal** (report it, drop the leg) — NOT a generic SafetyError
  hard-stop. Do not promise the user a pre-send minimum check; do not hardcode a magic minimum.
- **Spot-only, no short.** A "position" is the base-asset balance; `positions` reads non-zero balances.
  You cannot sell what you don't hold.
- **Buy by value** = market-only (`buy(symbol, cash_amount=)` via `createMarketBuyOrderWithCost`); LIMIT
  needs `quantity`. **Sell by `quantity` only.** A `%`/`$` trim → quantity goes through
  **`python -m vizier trim-qty`** (don't do the division by hand — Rule #2): `{"current_qty": <live base
  balance from `positions`>, "pct": 30}` or `{"current_price": <get_quote>, "dollar_amount": 50,
  "current_qty": <balance>, "step": <market precision>}`. It rounds **down** (never oversells), caps at
  the held balance, and — pass `"ticker"` + `"tag"` — cross-checks `tranche-sell` so a tactical trim
  can't eat the core. Take the `%` against the **live `positions` balance**, not the (possibly stale)
  thesis `qty`. `close_position(symbol)` sells the **whole** base balance and has a **~30s** cooldown
  (not 45).
- **No `wait_for_fill`** → confirm fills by **polling `order_status`** until filled/timeout. Never
  re-call `close_position` to "check" (hits the cooldown guard).
- **Protective stop: prefer the EXCHANGE-NATIVE `stop_order` (Valet ≥0.6.0), soft stop as fallback.**
  `stop_order(symbol, side, quantity, stop_price, limit_price?)` places a resting **trigger order on
  the exchange** — it fires even when no agent is running, which is exactly what an unattended position
  needs. Mechanics to respect: most spot APIs (binance included) only offer **stop-LIMIT**, so pass
  `limit_price` (for a SELL stop, at or slightly below `stop_price`) and **disclose the gap risk** ("a
  violent gap can jump the limit and leave the stop unfilled"). Sized by base `quantity` only. The tool
  **refuses cleanly** where the exchange has no native stops — ONLY then fall back to the **SOFT
  skill-monitored stop**, with the old disclosure verbatim: *"protection is skill-managed (monitoring),
  not a resting stop order on the exchange"*, honest about WHEN it fires (in default confirmation mode
  only when Vizier is next invoked, NOT continuously; a real 24/7 soft watch needs armed autonomy plus
  a scheduled loop). Never let "monitoring" imply protection the posture doesn't provide.
- **Crypto-specific pre-mortem:** peg risk if quoting against `USDT`/`USDC` (check `stablecoin_supply`);
  token unlocks / network upgrades / governance / halving as the event-risk analogue to earnings
  blackout; liquidity/slippage — prefer LIMIT with a slippage band off the majors, market only on
  BTC/ETH/liquid majors.

---

## Order-type discipline (both)

- **Default = LIMIT with a max slippage band.** Market only for liquid large-caps/ETFs and BTC/ETH
  majors. A 25%-of-NAV market order into an illiquid name = a bad fill. Read `price_history` (equities) /
  `crypto_order_book` + `crypto_price_history` (crypto) to judge liquidity.
- **Partial / unfilled (LIMIT in illiquids):** `wait_for_fill` with a timeout (IBKR) / poll
  `order_status` (crypto). Filled all → proceed. Partial or nothing at timeout → `cancel_order` the
  rest, **record the thesis against the quantity ACTUALLY filled** (not the target — the
  `baseline_snapshot` and sizing reflect reality), and re-try only if price/conviction still justify.
  Never leave a working order dangling between sessions unjournaled (it becomes a phantom position).

## Building the `reconcile` input (avoid the double-buy guard's failure mode)

`reconcile` takes `{own_sent_orders, broker_positions, ticker}` and flags `double_buy_risk` when a buy
for `ticker` was sent inside the lag window. Each `own_sent_orders` item needs **`{ticker, side,
value|qty, timestamp, status}`**. The catch: the journaled `executed_orders` schema is only
`{side, ticker, value, venue, order_id}` — **no per-order `timestamp`, no `status`**. If you feed those
rows raw, a missing `timestamp` is treated as "recent forever" and a missing `status` never looks
terminal, so **every ticker you ever bought flags as in-flight forever** (you then either freeze all
re-buys or, worse, learn to ignore the guard and fire into the real lag).

Build the input correctly instead: run **`python -m vizier build-own-sent-orders --json '{"ticker":
"AAA"}'`** — it reads the decision log and emits `own_sent_orders` already shaped for `reconcile`
(per-order `timestamp` falls back to the decision's, `status` to `"filled"` since a journaled order is a
recorded fill). For a still-in-flight order not yet journaled, append a `status` from a fresh
`order_status(order_id)` poll. Best practice: journal each fill with a per-order `timestamp` + `status`
so the log rows are directly reconcile-ready (the day/per-run aggregators ignore the extra fields). Then
call `reconcile` with `"venue"` set so the lag window matches: crypto ~30s, IBKR ~45s (the 45s default is
the conservative side — it over-flags rather than under-flags — so it is safe on both). The Valet's
`DUPLICATE_WINDOW_SECONDS` (default 5s) is a much shorter, independent guard — **do not** lean on it as
your double-buy protection; that rests on `reconcile` + the fill confirmation. (When arming autonomy,
consider setting `DUPLICATE_WINDOW_SECONDS` ≥ the reconcile window.)

## Tool-name corrections (don't mis-call Scout)

- Regime/theme scan uses **`news_search(query)`** (free-text, no symbol). `news_digest(symbols)` and
  `calendar(symbols)` need a symbol list first (e.g. a portfolio scan).
- There is **no** `screen` or `peers`. Generate candidates with `market_movers` / `crypto_movers`,
  `sector_performance` / `crypto_sectors`, `etf_holdings`, `retail_buzz` / `crypto_buzz`, `news_search`,
  `filing_search`. Build dossiers with `company_dossier` / `crypto_dossier`.

## Valet env backstops (set/confirm when arming autonomy — second, independent line)

Shared (in the venue's quote currency): `MAX_ORDER_VALUE` (per-order, default 100), `MAX_DAILY_VALUE`
(cumulative daily BUY cap — empty = **no cap**, so SET it), `DUPLICATE_WINDOW_SECONDS` (reject identical
orders within the window). IBKR live gate: `TRADING_ALLOW_LIVE` (+ `TRADING_DRY_RUN`). Crypto live gate
(independent): `CRYPTO_ALLOW_LIVE` + `CRYPTO_TRADING_MODE=live` + consciously `CRYPTO_DRY_RUN=false`;
spot lock `CRYPTO_ALLOW_MARGIN=false`. **Arming IBKR does NOT arm crypto, and vice-versa.** These Valet
guards are per-order/stateless — portfolio-level and cross-run safety is the skill's `autonomy-gate`.
