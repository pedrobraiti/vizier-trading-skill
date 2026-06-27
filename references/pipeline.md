# The reasoning pipeline — subagent fan-out

The pipeline is a **fan-out of subagents** (the Agent tool), not a rigid workflow. Adapt depth to the
request: a quick read = one analyst pass; a real money decision = the full chain below. Borrowed
deliberately from TradingAgents (roles) and ai-hedge-fund (persona lenses) — but with **their data
calls swapped for Scout tools and their execution swapped for Valet tools**.

```
                 ┌──────────── Analysts (parallel) ────────────┐
  candidates ──► │ Fundamental · Technical · News-sent · Macro │ ──► Bull × Bear ──► Trader
                 └─────────────────────────────────────────────┘                      │
                                                                                       ▼
   execute? ◄── Pre-mortem ◄── Risk gate + PM ◄── data-sufficiency gate ◄── thesis + trade proposal
```

**Research mode stops at "Trader proposes."** Execution mode continues through the gates to the Valet.

---

## Stage 0 — regime & candidates (before the per-name deep dive)

- Regime: `macro_context` (rates, VIX), `sector_performance` / `crypto_sectors`, `news_search(theme)`,
  `crypto_macro` / `crypto_fear_greed` for crypto. Check the **circuit breaker** here:
  `python -m vizier drawdown --json '{"window_days":30}'` returns `current_drawdown_pct` (running-peak-to-
  latest) and `max_drawdown_pct` — feed **`current_drawdown_pct`** as the breaker's `monthly_drawdown_pct`
  (the breaker wants "down how much right now", not the worst dip; in a recovered book they differ). If
  `drawdown.samples < 2` the leg has no data — say "drawdown leg: insufficient NAV history", don't read
  the `0.0` as a clean breaker. The `vix` leg is **equities-only**: for a crypto-only decision pass
  `vix: null` (do NOT feed DVOL/Fear-&-Greed into the `vix` slot — they have different scales and will
  mis-trip it); use `crypto_implied_vol`/`crypto_fear_greed` as qualitative pre-mortem context instead.
- Candidates: `market_movers` / `crypto_movers`, `etf_holdings`, `retail_buzz` / `crypto_buzz`,
  `filing_search`, `news_search`. (No `screen`/`peers` exist.)

## Stage 1 — Analysts (fan out in parallel, one subagent each)

Each reads Scout (data, not verdict) and returns a tight, sourced read **for BOTH horizons**.

- **Fundamental** — `company_dossier`, `fundamentals`, `sec_financials`, `quality_metrics`,
  `dividends`, `ownership`. Crypto: `crypto_dossier`, `crypto_asset_profile`, `crypto_onchain`,
  `defi_overview`/`stablecoin_supply`. *Prompt seed:* "Quality, durability, valuation context. What is
  the long-term fundamental case and its single biggest hole? Cite multiples/figures with `as_of`."
- **Technical** — `technicals`, `price_history`, `relative_strength`, `options_volatility`. Crypto:
  `crypto_technicals`, `crypto_price_history`, `crypto_order_book`, `crypto_implied_vol`,
  `crypto_derivatives`. *Prompt seed:* "Trend, momentum, overbought/oversold, support/resistance,
  liquidity. Short-term entry/timing read. Numbers, no verdict-as-fact."
- **News-sentiment** — `news` / `news_search`, `analyst_view`, `retail_buzz`/`crypto_buzz`,
  `wikipedia_attention`. *Prompt seed:* "What's the narrative and is it shifting? Consensus +
  price-target relay (as third-party data). Attention spikes?"
- **Macro** — `macro_context`, `world_macro`, `treasury_data`, `sector_performance`. Crypto:
  `crypto_macro`, `crypto_fear_greed`. *Prompt seed:* "Who benefits / is hurt by this regime? Rate /
  cycle / dominance sensitivity for this name's horizon."

## Stage 2 — Bull × Bear (debate the thesis)

Two subagents over the SAME analyst evidence. **Bull:** strongest case to own it. **Bear:** strongest
case against / what breaks it. Start single-round; escalate to multi-round only when the call is close
or the stake is large. Output: the crux of disagreement and which side carries the evidence.

## Stage 3 — Trader (propose)

Synthesize into ONE proposal: **thesis in one line**, horizon **tag** (`core`|`tactical`), **conviction
1-5** (5 = all four analysts confirm + Bull wins + a dated catalyst + no pre-mortem red flag; 3 = mixed
signals; 1 = an explicit order with no edge), proposed trade (venue, side, rough size), the dated
catalyst, the main risk, and the review trigger (price|date|fundamental + `is_hard_stop`). **Research
mode ends here** — present long & short reads, mark divergence, and stop.

## Stage 4 — data-sufficiency gate (mandatory before sizing)

```bash
python -m vizier data-sufficiency --json '{"scout_responses": {"price":..., "pe":..., "sector":...}, "decision_type": "valuation"}'
```
`abstain` → don't size (even under an explicit order — say so honestly). `downsize` → cut conviction/
size. `proceed` → continue.

**Pick the right `decision_type`, and require it to pass — the verdict is only as honest as the type you
ask for** (each type has its own minimum; relabelling thin data to a cheaper type green-lights it):
- **Equity buy** must clear **`valuation`** (price + ≥1 multiple + sector) **AND `technical`**
  (non-empty `price_history`). `valuation`'s P/E-style multiple + sector do **not** exist for crypto, so
  do NOT use it there.
- **Crypto buy** → use **`technical`** (and `macro` where relevant); never `valuation`.
- **Populate `scout_responses` from the RAW Scout envelope** — a null field stays `null`, an empty
  series stays `[]`. Never backfill from memory; that defeats the gate. (Scout returns `{ok:true}` with
  null fields when rate-limited — this gate is what stops that from looking like good data, but only if
  you feed it the raw nulls.) For a tiny explicit order on a liquid large-cap/ETF, a single re-fetch (or
  a Valet `get_quote`) before abstaining is fine — abstain only if the price truly can't be obtained.

## Stage 5 — Risk gate + Portfolio Manager (sizing within limits)

- Size: `python -m vizier size --json '{"slot_base":<base>, "conviction":n, "nav":NAV}'`, or for a fixed
  budget across N names `python -m vizier allocate --json '{"total_amount":100, "candidates":[...], "nav":NAV}'`.
  - **`slot_base` = the full-size slot a max-conviction (5) position gets = the per-asset cap =
    `NAV * max_pct_per_asset/100`** (`size` scales it by `conviction/5` and re-caps at the same per-asset
    limit). This is the deterministic default; do not invent the number. **(Manager: I picked
    per-asset-cap as `slot_base` — flagged for ratification.)**
  - **When the user named an exact dollar amount** ("buy $3 of AAPL"), DON'T call `size` — pass that
    amount straight to `limits` and the order. `size`/`allocate` are only for unspecified amounts.
  - **Explicit order below the conviction floor:** `size`/`allocate` silently **drop** a sub-floor leg
    (floor = 2). For a named imperative on a specific ticker, pass **`"explicit_order": true`** so it is
    honored — then flag the low conviction in the output (don't fake enthusiasm).
- Limits: `python -m vizier limits --json '{"portfolio":{"nav":..,"cash":..,"positions":[...]}, "candidate":{"ticker":..,"value":..,"sector":..}}'`.
  (For a CURRENT over-weight scan, call it with `"value": 0` per held ticker/sector — see the rebalance
  rule in SKILL.md.) If a leg violates max-position/sector/min-cash or collides with max-%/asset on a
  small account (Tension C): **downsize/drop a leg and confirm ONCE**, explaining the conflict — never
  breach a limit silently, never freeze the request. To get the compliant size, re-run `size`/`allocate`
  (which apply the cap) rather than back-solving the number by hand.
- Correlation with the current book: `correlation_matrix` / `classify` / `compare` — avoid stacking the
  same bet.
- Tie-breaking among candidates is **your qualitative judgment** (risk/reward, lowest correlation,
  nearest catalyst, thesis quality) — not a rigid formula. Cut conviction < 2 unless explicitly ordered.

## Stage 6 — Pre-mortem / red-team (one subagent, single mission)

"Argue why THIS trade, AT THIS MOMENT, is a mistake." Distinct from Bull×Bear (which is about the
thesis) — the pre-mortem is about **timing/execution**: earnings/event blackout in the horizon window
(`earnings`/`calendar`; crypto: unlock/upgrade/governance/halving), liquidity, crowdedness, a stop too
tight. Its findings appear to the user with the recommendation.

## Stage 7 — Execute (execution mode only)

Hand to `references/execution-mechanics.md` for the venue order flow and `references/autonomy-and-safety.md`
for the arming/gate discipline. Then journal the thesis (`write-thesis`, with `baseline_snapshot`) and
the decision/fills (`append-decision`), record a `nav-snapshot`, and produce the output
(`references/output-template.md`).

---

## Personas (roadmap, optional depth)

A "depth" dial: run famous-investor lenses (Buffett/Burry/Damodaran/Lynch/Munger/Ackman/Wood/Taleb…) as
parallel subagents over the same Scout evidence and synthesize. Not in v1 — a button for later.

## Heavy narrative → reuse `deep-research`

For "what's happening in the world / this sector this week", invoke the installed **`deep-research`**
skill (fan-out search → adversarial verify → cited synthesis). Don't expect Scout to do narrative
research — Scout gives `extract(url)` for cheap primary sources; deciding what to read and concluding is
yours. Cover the gaps the benchmark exposed: explicit caveats, quantified bear/base/bull scenarios,
comparison/price-target tables, primary sources (SEC, releases) over aggregators.
