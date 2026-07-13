# Canonical trace — "make 3 investments totaling $100"

> **`python -m vizier …` = the core interpreter resolved in SKILL.md** (the skill's bundled `.venv`
> python), never bare system `python`.


The reference flow. When a request looks like this ("research the market and make 3 investments of
$100"), walk these steps. It exercises every part of the design; deviate only with reason.

> Intent = **research + explicit execution**. Mode = whatever is active (default **confirmation**).
> **What exactly is the contract here?** The **$100 total and the count of 3** — those the gates only
> ANNOTATE, never shrink. The **names are skill-derived** (Vizier picks them), so the gates MAY prune
> or downsize an individual leg (backfill from the shortlist to keep the count); on a leg the USER
> named, they would only annotate. Signal low conviction honestly either way.

1. **Classify intent → execution authorized.** Identify venue per eventual ticker (equities → `ibkr`,
   crypto → `crypto`).

2. **Pre-flight (Valet).** `session_status` (assert `account_type` matches the mode), `market_status`,
   `portfolio` + `account_summary` (NAV = limit denominator; free cash = deployable). If not logged into
   IBKR and the candidates are equities → say so and degrade (research-only) rather than fail.

3. **Regime + breaker (Scout + core).** `macro_context`, `sector_performance`, `news_search(theme)`
   (crypto: `crypto_macro`/`crypto_fear_greed`). Record a `nav-snapshot` **with its `venue`**. Check the
   breaker (feed the breaker's `monthly_drawdown_pct` from `drawdown`'s **`current_drawdown_pct`**, not
   `max_drawdown_pct`; pass the **`venue`** — NAV is per venue and a mixed series is refused;
   `vix` is equities-only — pass `null` for a crypto-only batch):
   ```bash
   python -m vizier drawdown --json '{"window_days":30, "venue":"ibkr"}'  # -> current_drawdown_pct (+ max)
   python -m vizier breaker  --json '{"vix": <from macro_context, or null for crypto>, "monthly_drawdown_pct": <current_drawdown_pct>}'
   ```
   If tripped: in confirmation, ask once ("market in panic — still want the 3?"); in autonomy, abandon.

4. **Generate candidates = Stage B breadth discovery** (this request is broad → don't use the thin
   `market_movers` sweep, which yields correlated picks). As **manager**, read the regime, **partition
   the market into coverage areas**, dispatch research-only envoys by area (one parallel batch), then
   dedup and **funnel-prune with `correlation_matrix`/`crypto_correlation_matrix`** so the shortlist is
   DIVERSIFIED, not three of the same bet. Shortlist > 3 so the gates have room to drop one. (For a
   seeded/narrow request, fall back to the Stage 0 thin candidate line.) See `references/pipeline.md`
   Stage B.

5. **Per-candidate deep dive = the pipeline.** Fan out Analysts (parallel) → Bull×Bear → Trader proposes
   thesis + horizon tag + conviction (1-5), for BOTH horizons. (`references/pipeline.md`.)

6. **Data-sufficiency gate, each candidate.**
   ```bash
   python -m vizier data-sufficiency --json '{"scout_responses":{...},"decision_type":"valuation"}'
   ```
   `abstain` → drop the leg (honest). `downsize` → trim it. **Why dropping is legitimate HERE and not a
   contract breach:** in this request the NAMES are skill-derived — only the $100 total and the count of
   3 are the user's contract — so the gate may prune a derived leg and you backfill from the shortlist.
   On a leg the user NAMED (an explicit order), the gate only **annotates** (honest caveat) and the
   user's amount still executes (SKILL.md, "gates annotate, they don't prune").

7. **Risk gate + sizing.** Allocate the $100 by conviction and cap per asset. **Do NOT pass the
   call-level `"explicit_order": true` here** — this slate is 100% skill-derived, and that flag
   floor-exempts YOUR OWN ideas (exactly what SKILL.md warns against; it is for user-NAMED legs, tagged
   per-candidate `"explicit": true` in a mixed slate). The user's contract is the $100 total and the
   count of 3, not the names: the conviction floor may drop a sub-floor pick of yours — backfill from
   the >3 shortlist so the count still lands. **This single `allocate` is correct only because all
   three survivors are on ONE venue (`ibkr` equities), sized against the IBKR NAV:**
   ```bash
   python -m vizier allocate --json '{"total_amount":100,"nav":<IBKR_NAV>,"candidates":[{"ticker":"AAA","conviction":5},{"ticker":"BBB","conviction":3},{"ticker":"CCC","conviction":2}]}'
   ```
   **Mixed-venue slate** (say AAA/BBB on `ibkr`, conviction 5+3=8, plus a `crypto` BTC/USDT leg,
   conviction 4): do NOT pass one `total_amount`/`nav` across both — that blends two NAVs under one
   denominator (B4 forbids it). First **partition the $100 by aggregate conviction per venue** — 8/12 →
   $66.67 to equities, 4/12 → $33.33 to crypto (tie → split by leg count) — then run **two** `allocate`
   calls, each with its venue's slice as `total_amount` and that venue's own NAV:
   ```bash
   python -m vizier allocate --json '{"total_amount":66.67,"nav":<IBKR_NAV>,"candidates":[{"ticker":"AAA","conviction":5},{"ticker":"BBB","conviction":3}]}'
   python -m vizier allocate --json '{"total_amount":33.33,"nav":<CRYPTO_NAV>,"candidates":[{"ticker":"BTC/USDT","conviction":4}]}'
   ```
   Then check each leg against the book:
   ```bash
   python -m vizier limits --json '{"portfolio":{"nav":<NAV>,"cash":<cash>,"positions":[...]},"candidate":{"ticker":"AAA","value":<size>,"sector":"<sector>"}}'
   ```
   A violation (max-position/sector/min-cash, or a tiny account hitting max-%/asset → **Tension C**):
   downsize/drop a leg and **confirm once**, explaining — never breach silently, never freeze.
   Floor: a skill-derived leg under conviction 2 is dropped (backfill to keep the count); only a
   USER-NAMED leg overrides the floor — then keep it, flag the low conviction, and tag it
   per-candidate `"explicit": true`.

8. **Pre-mortem, each surviving leg.** "Why is THIS trade, NOW, a mistake?" Earnings/event blackout
   (`earnings`/`calendar`; crypto: unlocks/upgrades/halving), liquidity, crowding. Surface findings.

9. **Execute / confirm / record (per the mode) — journal EACH leg before starting the next.**
   - **Confirmation:** present the plan (output template), wait for OK, then per leg: re-verify
     `session_status`, `reconcile`, **IBKR** `preview_order` → `buy(cash_amount=)` (market) → confirm via
     `order_status`/`wait_for_fill` → then **read `positions` for the exact held quantity and size the
     stop POST-fill with `exit-qty`** (NEVER from `filled_quantity`: on a US$ order IBKR reports the fill
     in DOLLARS — a $2 AAPL buy came back as `2.0` against 0.0063 shares held, and a 2-share stop on it
     is a naked short); **crypto** estimate
     via `get_quote` + check the notional minimum → `buy(cash_amount=)` → poll `order_status` → place the
     **exchange-native `stop_order`** POST-fill (stop-LIMIT: `limit_price` at/just below the stop;
     disclose the gap risk; base qty from `exit-qty` against the `positions` balance) — soft
     skill-managed stop ONLY if the venue refuses native stops.
     **Then, still on THIS leg: `append-decision` the fill + `write-thesis` (with its
     `baseline_snapshot`, the filled `qty` in SHARES/base units, and the USD in `cash_qty`) — only then
     move to the next leg.** Never batch the
     journaling to the end of the round: a crash between leg 2 and leg 3 must leave two journaled fills,
     not two phantom positions (SKILL.md safety rules; same discipline as autonomy).
   - **Autonomy:** run the arming checklist once, `begin-run` at the start of this round, then per leg
     `autonomy-gate` before the order and `append-decision` the fill after — before gating the next
     candidate. The gate enforces BOTH the per-run cap (33%/5 of this batch) and the daily ceiling.
     (`references/autonomy-and-safety.md`.)

10. **Session-end output.** The three theses and fills are already journaled (step 9, per leg) — now
    record the `nav-snapshot`, then the TL;DR-first output (`references/output-template.md`) — long &
    short per name, conviction n/5 (flag the low ones), scenarios + caveats + pre-mortem, post-trade
    exposure, sources. Commit memory if configured (`--commit`).

> Note on Scout scope: `screen`/`peers` are roadmap and don't exist — candidates come from
> `market_movers` + dossiers. Never add anything to Scout to make this flow nicer; solve it here.
