# Autonomy & safety — the §B discipline

> **`python -m vizier …` = the core interpreter resolved in SKILL.md** (the skill's bundled `.venv`
> python), never bare system `python`.


Autonomy is the dangerous mode. It is **opt-in only** (the user explicitly says "execute without
asking"), and arming it is a checklist, not a vibe. The deterministic core (`python -m vizier`) holds
the math; this file is the order in which you call it and the Valet, and the rules around it.

> **What these guards are for, and how binding they actually are (be precise).** The §B ceilings and the
> drawdown-kill exist to **catch the ROBOT going haywire while no human is watching** — they are a
> failure-prevention backstop for armed, unattended autonomy, **not** a brake on a human's confirmed
> explicit order. They never apply to the confirmation path; do not let them clamp or second-guess a named
> human order. And on bindingness, don't overstate it: Vizier's ceilings + drawdown-kill are
> **advisory/bookkeeping — they only bind if the skill actually CALLS `autonomy-gate` each candidate and
> feeds it an honest live NAV.** The arithmetic is exact and self-latching in code, but nothing in Vizier
> can physically stop an order the LLM never routed through the gate. The **one hard, code-enforced dollar
> backstop is the Valet's `MAX_DAILY_VALUE`** (rejected at the executor regardless of what the skill does)
> — which is exactly why arming is FORBIDDEN until it is set. Treat Vizier's gate as the disciplined
> bookkeeper and the Valet env caps as the hard floor under it.

> **Why this exists:** the per-run cap alone does NOT stop a loop from draining the account — each round
> mobilizes a fresh slice of what's left. So there are TWO ceilings, both code-enforced from the
> decision log and both anchored to the SAME FIXED start-of-day baseline:
> - **per-run** (`per_run_pct` = 33% of NAV **OR** `per_run_max_trades`) — caps a single autonomous
>   round; resets every `begin-run`.
> - **cumulative-daily** (`daily_cumulative_pct` = 50% of NAV **OR** `daily_max_trades`) — caps the whole
>   24h window; keeps accumulating across runs (the drain fix).
> Plus a **drawdown kill**. The `autonomy-gate` command composes per-run + daily + kill + armed into one
> verdict. Your job is to feed it honestly — `begin-run` each round and journal every fill.

---

## Arming checklist (do ALL of this, in order, before the first autonomous order)

1. **Fix the day baseline.** Read NAV (`account_summary.net_liquidation` for the venue) and:
   ```bash
   python -m vizier arm-autonomy --json '{"nav": <NAV>}'
   ```
   This writes the fixed baseline + 24h window to private memory. It is also the **manual re-arm**
   point after a kill.
2. **Set/confirm the Valet backstops** (a second, independent line — the Valet's guards are
   per-order/stateless, default-off): `MAX_DAILY_VALUE` (it defaults to *no cap* — SET it),
   `DUPLICATE_WINDOW_SECONDS`, `MAX_ORDER_VALUE`. **Autonomy is FORBIDDEN if `MAX_DAILY_VALUE` is still
   at its default (no cap).**
3. **Confirm the venue-specific live gate** (arming one venue does NOT arm the other):
   - IBKR: `TRADING_ALLOW_LIVE=true` (+ `TRADING_DRY_RUN` as intended).
   - Crypto: `CRYPTO_ALLOW_LIVE=true` + `CRYPTO_TRADING_MODE=live` + consciously `CRYPTO_DRY_RUN=false`.
     (Not every exchange has a sandbox — in `live`, even with dry-run, you are one step from real money.)
4. **Assert paper/live truth** via `session_status` and confirm **`account_type`** (`"PAPER"`/`"LIVE"` —
   the field present and identically named on both venues; do not key off the venue-specific `isPaper`/
   `is_paper` boolean) matches the mode you intend. The "shadow" run must not think it's paper while
   logged into LIVE.

> **Re-arm discipline (the drain trap), now CODE-enforced.** Re-arming mid-window would reset the day
> baseline and wipe `spent_today`, handing a fresh 50%-of-NAV budget — defeating the cumulative ceiling.
> So **`arm-autonomy` REFUSES** while a window is active: it returns `{"ok": false, ...,
> "error_type": "AutonomyAlreadyArmedError"}` and does not touch the state. A legitimate re-arm runs only
> after an explicit **`disarm-autonomy`** (the post-kill path disarms first) or after the 24h window
> expires — there is deliberately no force-override. When you DO re-arm post-kill at a depressed NAV, the
> drawdown-kill floor moves DOWN (15% of the lower NAV): state the new kill level and have the user
> acknowledge the kill cause is resolved before continuing.

## Per-round + per-candidate loop (while armed)

**At the start of EACH autonomous round** (batch of candidates), mark the run — this resets the per-run
counter; without it the gate refuses (no run marker = no per-run safety):
```bash
python -m vizier begin-run --json '{}'
```

Then, **for each candidate in the round:**

1. **Re-verify** `session_status` (assert `account_type`) and **re-check the circuit `breaker`** — the
   breaker is **separate** from `autonomy-gate` (the gate does not see VIX/monthly-drawdown), so it is a
   distinct call here, every candidate.
2. **Reconcile** (`reconcile`) own sent orders ∪ positions (build the input per
   `references/execution-mechanics.md`); if a buy for this ticker is in-flight, poll its fill first.
3. **Gate the candidate** with a FRESH live NAV read from `account_summary.net_liquidation` (never reuse
   the baseline or a cached value — the drawdown kill is only as honest as this number):
   ```bash
   python -m vizier autonomy-gate --json '{"candidate_value": <USD>, "current_nav": <FRESH live NAV>}'
   ```
   `allowed:false` → **do not send**; surface the `blocks` (`not_armed`, `per_run_ceiling`,
   `cumulative_ceiling`, `drawdown_kill`) and stop the relevant scope. A `per_run_ceiling` with "no
   active run" means you skipped `begin-run`. **A `drawdown_kill` block is NOT a skip-and-continue** —
   see "When something trips" below: abandon the rest and disarm. `allowed:true` → proceed to the venue
   order flow (`references/execution-mechanics.md`). (`autonomy-state` shows both the daily and per-run
   numbers.)
4. **Journal the fill — non-negotiable, before gating the NEXT candidate.** Immediately after the order
   fills, append it with the exact execution schema, because this is what BOTH ceilings read next. Add a
   per-order `timestamp` (the fill time) and `status` so the row is also `reconcile`-ready:
   ```bash
   python -m vizier append-decision --json '{"intent":"buy AAA","mode":"autonomy","executed_orders":[{"side":"BUY","ticker":"AAA","value":200.0,"venue":"ibkr","order_id":"<id>","timestamp":"<fill ISO-8601>","status":"filled"}]}'
   ```
   **If you don't journal fills, the §B guarantee goes hollow again** — the gate will under-count spend
   and re-authorize slices it shouldn't. Only `side:"BUY"` value consumes the spend ceiling; any executed
   order counts as a trade. The order is strict: **gate → send → confirm fill → journal → only then gate
   the next candidate** (never batch the journaling to the end of the round).

## When something trips mid-batch

- **`drawdown_kill`** (NAV fell past the kill threshold from the day baseline): the gate returns it as a
  `block` but **does NOT disarm for you** — it looks like any other `allowed:false`. You must branch on
  `block.type == "drawdown_kill"`: **abandon ALL remaining candidates in the batch, call
  `python -m vizier disarm-autonomy`, escalate to manual.** This is different from a `cumulative_ceiling`/
  `per_run_ceiling` block (which only stops the current scope, autonomy stays armed). Autonomy requires a
  deliberate `arm-autonomy` re-arm — never auto-continue, never auto-re-arm (and re-arm only per the
  re-arm discipline above).
- **Circuit breaker trips** in autonomy: same — abandon remaining orders, log, escalate. (In
  confirmation mode the breaker asks once instead.)
- **Any Valet rejection / SafetyError:** hard stop, log "blocked, not executed", no auto-retry.

## Shadow / paper-first ladder (the birth posture — don't skip rungs)

1. **Shadow** — decide fully, **journal the decision instead of sending** (or send with `CRYPTO_DRY_RUN`/
   `TRADING_DRY_RUN` on). Forward-test for weeks; compare against the real market.
2. **Paper / testnet** — execute against the paper account / exchange sandbox (`isPaper` true). Confirm
   the plumbing end to end.
3. **Live read-only validation** — point at the live account but read-only; confirm balances/quotes/
   session behave.
4. **Real-money autonomy** — only after 1-3 earn confidence, with the full arming checklist, and even
   then ideally small. This is a conscious promotion, command by command — never the default.

## Confirmation mode (the default — no arming, human gates)

No `arm-autonomy`, no daily ceiling needed (the human is the gate). Still do the per-order safety:
re-verify session (`account_type`), re-check the breaker (it asks **once** on an explicit order — "market in
panic, still want the 3?"), reconcile, run the data-sufficiency + limits gates, show the decision, wait
for the OK. Calibration stays: "buy $3 of AAPL" executes after the gates without re-confirming the
obvious; only REAL ambiguity pauses to ask.

**Breadth discovery does not change the gates.** The Stage B envoys are research-only (they cannot
trade), and every survivor still passes data-sufficiency → limits → breaker → (if armed) autonomy-gate
**per candidate on the main thread**. The funnel ranks and prunes; it never pre-authorizes a trade.

## Precedence (when rules collide)

**Risk rules (stop, breaker) > anti-churn > horizon tags.** A pre-committed stop firing is never churn.
The kill and the breaker outrank an explicit order (they confirm/abandon, they don't obey blindly).
