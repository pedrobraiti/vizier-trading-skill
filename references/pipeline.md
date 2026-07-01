# The reasoning pipeline — subagent fan-out

> **In every `python -m vizier …` below, `python` = the core interpreter you resolved in SKILL.md**
> (the skill's bundled `.venv` python) — never bare system `python`. And never co-batch a core/Valet
> call with always-allow Scout calls in one parallel block (see SKILL.md).

The pipeline is a **fan-out of subagents** (the Agent tool), not a rigid workflow. Adapt depth to the
request: a quick read = one analyst pass; a real money decision = the full chain below. Borrowed
deliberately from TradingAgents (roles) and ai-hedge-fund (persona lenses) — but with **their data
calls swapped for Scout tools and their execution swapped for Valet tools**.

```
 broad-discovery?  ┌──────── Breadth discovery (MANAGER, Stage B) ───────┐
   regime read ──► │ partition by AREA → N research-only envoys → dedup  │ ──► candidate
                   │ → funnel-prune (correlation-diverse, venue-separate) │      slate
                   └─────────────────────────────────────────────────────┘       │
                                                                                  ▼
                 ┌──────────── Analysts (parallel, by ROLE) ───┐
  candidates ──► │ Fundamental · Technical · News-sent · Macro │ ──► Bull × Bear ──► Trader
                 └─────────────────────────────────────────────┘                      │
                                                                                       ▼
   execute? ◄── Pre-mortem ◄── Risk gate + PM ◄── data-sufficiency gate ◄── thesis + trade proposal
```

**Research mode stops at "Trader proposes."** Execution mode continues through the gates to the Valet.
*(In breadth discovery, research mode stops at the ranked slate AND each survivor's Trader proposal —
the full per-name read short of execution, exactly as B5 hands off; the envoys are ALWAYS research-only,
regardless of mode — only the main orchestrator thread ever executes.)*
*(In read-only mode the data-sufficiency gate may still run, but only to **annotate** each name's
conviction/quality — it never gates a name out of a requested recommendation count; the count is the
contract. See Stage 4 and B4 step 5.)*

---

## Stage 0 — regime & candidates (before the per-name deep dive)

**Stage 0a — portfolio-aware brake (REQUIRED for any autonomous, vague, or manager/breadth flow).** Before
proposing a single trade in a flow where *Vizier* chose to act (not a human's explicit named order), you
MUST pull the live book first — Valet `portfolio` + `positions` + `account_summary` (and `reconcile` for
in-flight orders), per venue — and reason **explicitly over it**: given what's already held, the
diversification, the cash on hand and the existing theses, is this new trade actually additive, or is the
right move to do **nothing / less**? This portfolio-aware judgment IS the overtrading defense — there is no
"max N trades" cap doing that job for a human at the wheel; the brake is this reasoning. "No edge / nothing
worth doing — holding" is a complete, first-class outcome. Skip this only for a direct explicit named order
(there the user already decided). The numeric §B ceilings are a *separate* unattended-robot backstop, not
this brake.


- Regime: `macro_context` (rates, VIX), `sector_performance` / `crypto_sectors`, `news_search(theme)`,
  `crypto_macro` / `crypto_fear_greed` for crypto. Check the **circuit breaker** here:
  `python -m vizier drawdown --json '{"window_days":30}'` returns `current_drawdown_pct` (running-peak-to-
  latest) and `max_drawdown_pct` — feed **`current_drawdown_pct`** as the breaker's `monthly_drawdown_pct`
  (the breaker wants "down how much right now", not the worst dip; in a recovered book they differ). If
  `drawdown.samples < 2` the leg has no data — say "drawdown leg: insufficient NAV history", don't read
  the `0.0` as a clean breaker. The `vix` leg is **equities-only**: for a crypto-only decision pass
  `vix: null` (do NOT feed DVOL/Fear-&-Greed into the `vix` slot — they have different scales and will
  mis-trip it); use `crypto_implied_vol`/`crypto_fear_greed` as qualitative pre-mortem context instead.
- **Direction needs a series, not a level.** `macro_context` gives a SNAPSHOT (rate level, VIX level). Do
  NOT infer a direction ("Fed cutting", "rates rising") from a level alone — state the level ("policy rate
  ~3.6%") and pull the directional series (`treasury_data`/`world_macro`/rate history) before claiming any
  trend. No series → the direction is unknown; say so. (SKILL.md "Macro direction needs a directional series".)
- Candidates (**narrow / seeded requests only** — a single name, a named sector, or a user-supplied
  list): `market_movers` / `crypto_movers`, `etf_holdings`, `retail_buzz` / `crypto_buzz`,
  `filing_search`, `news_search`. (No `screen`/`peers` exist.) **For a BROAD discovery request do NOT
  use this thin sweep** — it draws one lens (mostly `market_movers`, whose daily gainers/losers are
  themselves correlated) and yields a correlated shortlist; use **Stage B** below instead.

## Stage B — Breadth discovery (manager fan-out by market AREA)

**Only for a class-2 broad-discovery request.** This is the candidate ENGINE that feeds Stage 1 — it
*precedes* the depth pipeline, it does not replace it. Skip it entirely for a single-name or seeded
request (use the Stage 0 narrow line). Here you act as the **manager of a research team**, not as an
analyst: you decide who looks where, then prune what they bring back.

**B0 — Manager macro-read.** From the Stage 0 regime read (`macro_context`, `sector_performance`/
`crypto_sectors`, `news_search(theme)`, `crypto_macro`/`crypto_fear_greed`) write a **one-line regime
statement** (e.g. "rates higher-for-longer, VIX ~14 calm, crypto risk-on under BTC dominance"). This
line is INPUT to the partition and is pasted into every envoy dispatch so envoys hunt *for this regime*,
not in a vacuum.

**B1 — Partition the market into coverage AREAS (regime-informed).** Carve the universe into **disjoint**
areas. For an **unscoped** broad sweep the default universe spans **US equities/ETFs + crypto spot**; when
the user **explicitly scopes** the request (asset class, sector, or theme), the partition MUST honor that
scope — see the scope rule below. Mix the axes — don't just list GICS sectors:
- by **sector** (tech, energy, financials, healthcare, industrials, staples/defensives, …)
- by **theme** (AI/datacenter, rate-sensitives, reshoring, GLP-1, power/uranium, …)
- by **style/factor** (quality compounders, deep-value/turnarounds, high-momentum, dividend/defensive)
- by **asset class** (crypto majors BTC/ETH; crypto themes — L1/L2, DeFi, stablecoin/RWA)

Bias the partition toward where the regime says edge lives. **For an unscoped broad sweep, always include
≥1 contrarian/defensive area and ≥1 crypto area** so the slate cannot collapse into one risk-on cluster
(this, with B4, is the direct fix for "3 of the same bet"). **When the user explicitly scopes the request**
— asset class ("ações"/stocks/equities/ETFs only, or conversely crypto-only), sector, or theme — that
scope OVERRIDES the diversity seed and the equities+crypto default: narrow the areas to the scope and
**SUPPRESS the forced off-scope areas** (no crypto area for a stocks-only request; no off-theme or forced-
contrarian areas for a themed request like "AI/datacenter" or "energy"). The diversity seed is the default
for **open/unscoped** requests only — never a reason to return an off-scope name the user didn't ask for.
Areas must be **mutually exclusive** — give each envoy an explicit
boundary ("you own ENERGY: large-cap E&P, oil services, refiners; do NOT cover utilities or uranium —
that's the Power envoy"). An unavoidable cross-area name (an ETF, a conglomerate) is resolved at the B4
merge, never by overlapping mandates.

**B2 — Team-size scaling (your call as manager).** Match the envoy count to the request's breadth:
- **Light (3-4)** — "any ideas?", a quick look. Broad buckets: equity-cyclical · equity-defensive/
  secular · crypto.
- **Balanced (5-6, DEFAULT for "bring me recommendations")** — finer sector/theme split + a style
  envoy + 1-2 crypto envoys.
- **Exhaustive (8-12)** — "sweep everything". Full sector ladder + style factors + crypto-sector split +
  macro-theme envoys. Cap the team at **12** even for "sweep everything" — more envoys is diminishing
  coverage at linear token cost.

**Announce the team size before spending it.** At dispatch, tell the user how many envoys you're about to
spin up and why ("spinning up 6 area researchers — balanced sweep across sectors, a style factor and
crypto"), so the multi-agent token cost is a stated choice, not a silent surprise. One line, then go.

Run all envoys for a tier in **ONE parallel batch** (a single message, multiple Agent calls). Never
chain them serially — that is the token/latency blow-up.

**B3 — Envoy dispatch (one subagent per area).** Spawn each envoy as the **`vizier-research-envoy`**
agent type when installed — its frontmatter withholds the Valet execution tools, so the envoy *cannot*
trade (the hard firewall). If that type is not installed, spawn a standard subagent and, in the dispatch,
grant only Scout research tools and **never mention the Valet tools** (the soft layer; "only the
orchestrator executes" still holds either way). Dispatch prompt seed:

> "You are a **research envoy** for coverage area **{area}** (venue scope: {ibkr equities/ETFs | crypto
> spot | both}). **Mandate: RESEARCH ONLY — you surface candidates, you do not trade.** Current regime:
> {regime line from B0}. Using **Scout tools only** (`market_movers`/`crypto_movers`, `sector_performance`/
> `crypto_sectors`, `etf_holdings`, `retail_buzz`/`crypto_buzz`, `news_search`, `filing_search`, and a
> LIGHT `company_dossier`/`crypto_dossier` peek), find the **2-4 most compelling LONG candidates in YOUR
> area for THIS regime**. This is a FIRST-PASS scan, NOT a deep dive (analysts handle depth later) — give
> a tight, cited case, don't exhaust every tool. **Stay strictly inside {area}.** If your area has no real
> edge right now, say so and return fewer (or none) — do not pad. Return each candidate as the row below,
> every figure tagged with its `as_of`."

**Structured candidate row (each envoy returns a list of these):**
```
- ticker:      canonical symbol (crypto BASE/QUOTE, e.g. BTC/USDT)
- area:        the coverage area (lets the manager track diversification)
- venue:       ibkr | crypto
- case:        one-line long thesis
- key_risk:    the single biggest hole / what breaks it
- conviction:  rough 1-5 FIRST-PASS (NOT the Stage 3 conviction — pre-deep-dive)
- evidence:    2-3 figures/signals, EACH with as_of
```

**B4 — Dedup / merge / funnel-prune (manager judgment — no new core command).** Collapse the envoy
returns into one slate, then prune:
1. **Dedup** — same ticker from two envoys → one row, keep the stronger case, note it surfaced in
   multiple areas (a mild positive, not a conviction multiplier).
2. **Score & cut the weak** — rank by potential × risk/reward (qualitative, per Stage 5's "your
   qualitative judgment"). Drop regime-mismatched and thin-evidence names. **When the user explicitly
   scoped a horizon** ("short-term, ≤1yr"), score **for that horizon**: a name attractive ONLY in the
   non-requested horizon is off-scope for the ranked picks — tag it "off-scope: long-term only" and keep
   it out of the scoped ranking, never rank it as a top short-term pick. The other horizon is still SHOWN
   as context (the multi-horizon mandate stands for presentation), but it earns no spot on the scoped list.
3. **Correlation-based diversification — THE fix.** Run `correlation_matrix` on the surviving **equity**
   shortlist and `crypto_correlation_matrix` on the **crypto** shortlist. Where two survivors are highly
   correlated, **keep the higher-conviction one and backfill from a lower-correlation candidate still on
   the slate**. Target: survivors span areas AND are not mutually correlated. There is **no cross-venue
   matrix** — for equity↔crypto co-movement apply a qualitative risk-on/off judgment (high-beta tech and
   BTC move together in risk-on) and flag it; never invent a number.
4. **Venue separation + budget split.** Rank the combined slate by merit for the RECOMMENDATION order,
   but keep risk per venue. A single user budget ("$100 across 3") spanning both venues must first be
   **partitioned across venues, THEN allocated within each**:
   - **Partition rule.** Split the budget across venues in proportion to each venue's **aggregate
     first-pass conviction** (Σ conviction of that venue's surviving legs ÷ Σ conviction of all
     survivors). E.g. equity legs summing to conviction 8 and a crypto leg of conviction 4 → 8/12 of the
     budget to equities, 4/12 to crypto. If the per-venue conviction sums **tie**, fall back to
     splitting by **leg count** per venue.
   - **Then allocate within each venue.** Run a **separate `allocate`** for each venue's legs, passing
     that venue's slice as `total_amount` and **that venue's own NAV** as `nav`. Never compare an IBKR
     size to a crypto one under one denominator, and never report a single blended cross-venue exposure.
5. **Funnel cap.** Hand only the best few survivors to Stage 1 (default ~3-5, or exactly the N the user
   asked for). **Execution mode only:** hand +1-2 spare so the gates have room to drop one. **Read-only
   research/recommendation mode:** there is no money at risk, so a named count N is a **CONTRACT** —
   deliver exactly N names. The data-sufficiency gate (and `limits`) then **ANNOTATE** each name (flag
   thin data / low conviction / "couldn't fully verify") and **NEVER remove a name from the requested
   count**. Do NOT run `limits` (an execution concentration check) to prune a pure recommendation slate.
   Two distinct limits — don't conflate them:
   - **Model-discretion ceiling (~6).** When the user did NOT name a count, never hand more than ~6 names
     into the depth pipeline regardless of tier or how many the envoys surfaced — no self-directed
     "analyze them all". Rank, keep the top ~6, mention the rest as also-rans. This is what stops the
     EXPENSIVE per-name depth pipeline from spontaneously running on dozens.
   - **An explicit user count OVERRIDES that ceiling** ("bring me 10 stocks"). In **read-only research /
     recommendation** mode there is no money at risk, so the requested count IS the deliverable — honor
     it. Announce the cost first ("10 names = a deep dive each, ~Nx the work — going ahead"), and for a
     large count you may **tier the depth** (full Analyst→Bull×Bear→pre-mortem on the strongest cluster,
     a lighter first-pass-plus read on the tail) rather than skimping all of them equally.
   - **Execution mode — judgment, not a hard cap.** When *Vizier* is choosing how many names to fund in a
     self-directed pass, ~6 is a sensible diversification/cost ceiling on its own discretion (rank, fund
     the best, mention the rest). But this is **not** a "max N trades" rule binding the user: an **explicit
     user list** ("buy these 8") is explicit intent — honor it faithfully, raising the single concentration
     caution ONCE if funding that many in one pass is genuinely a concern, then comply. A research-only
     count never auto-converts into money; that still takes the user's explicit go-ahead.

**B5 — Handoff.** The surviving slate IS the `candidates` arrow into Stage 1. From here the pipeline is
unchanged: each survivor runs Analysts → Bull×Bear → Trader → (execution mode) data-sufficiency → Risk/PM
→ pre-mortem → Stage 7. Research mode stops at the ranked slate + per-name Trader proposals; execution
mode continues through the gates **on the main thread only**.

**Unsolicited-crypto disclosure (execution mode).** Because B1 always seeds ≥1 crypto area, a slate built
for a user who never mentioned crypto can carry a crypto leg. If so, **flag every such leg explicitly at
the confirmation step and get the crypto venue okayed before any crypto order** ("1 of 3 is BTC/USDT on
the `crypto` exchange — soft skill-managed stop, separate account — include it?"). Keep crypto in the
RESEARCH universe regardless (the diversity is the point); the gate is only on *executing* an unsolicited
crypto leg. See SKILL.md safety rules + `references/output-template.md`.

## Stage 1 — Analysts (fan out in parallel, one subagent each)

Each reads Scout (data, not verdict) and returns a tight, sourced read **for BOTH horizons**.

- **Fundamental** — `company_dossier`, `fundamentals`, `sec_financials`, `quality_metrics`,
  `dividends`, `ownership` (now incl. short interest), `fda_events` (pharma/biotech approval/recall
  catalysts). Crypto: `crypto_dossier`, `crypto_asset_profile`, `crypto_onchain`, `btc_network` (BTC
  base-layer fundamentals + real NVT), `defi_fees` (protocol fees/revenue cash-flow),
  `defi_overview`/`stablecoin_supply`. *Prompt seed:* "Quality, durability, valuation context. What is
  the long-term fundamental case and its single biggest hole? Cite multiples/figures with `as_of`."
  **Capital-structure check (execution-grade theses):** Scout's free news feed (GDELT) is **not
  exhaustive** and routinely MISSES capital-structure events — equity raises, dilution/secondaries,
  buybacks, convertible issuance, M&A. Do a **dedicated recent-filings pass** (`filing_search` /
  `sec_financials` / `ownership`; crypto: `crypto_onchain` supply/unlocks, `crypto_asset_profile`)
  rather than trusting the news feed, and **report what you checked**. For crypto, treat token
  unlocks / emissions as the dilution analogue.
- **Technical** — `technicals`, `price_history`, `relative_strength`, `options_volatility`. Crypto:
  `crypto_technicals`, `crypto_price_history`, `crypto_order_book`, `crypto_implied_vol`,
  `crypto_derivatives`, `coinbase_premium` (US-spot demand). **Relative-value / pairs:**
  `cointegration_test` / `find_cointegrated_pairs` — mean-reverting spreads (hedge ratio, spread
  z-score, half-life); these MEASURE the relationship, the pair-trade decision is yours.
  *Prompt seed:* "Trend, momentum, overbought/oversold, support/resistance,
  liquidity. Short-term entry/timing read. Numbers, no verdict-as-fact."
- **News-sentiment** — `news` / `news_search`, `analyst_view`, `retail_buzz`/`crypto_buzz`,
  `wikipedia_attention`. *Prompt seed:* "What's the narrative and is it shifting? Consensus +
  price-target relay (as third-party data). Attention spikes?"
- **Macro** — `macro_context` (now incl. Fed net liquidity, financial conditions, claims, nowcasts,
  VIX term structure), `world_macro`, `treasury_data`, `sector_performance`, `cot_positioning`
  (speculator/commercial futures positioning), `commodity_ratios` (copper-gold / gold-silver
  bellwethers). Crypto: `crypto_macro`, `crypto_fear_greed`, `stablecoin_peg` (peg-stress).
  *Prompt seed:* "Who benefits / is hurt by this regime? Rate / cycle / dominance sensitivity for
  this name's horizon."

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
(The multiple key may be `pe` or Scout's native `pe_ratio` — the gate accepts both.)
`abstain` → don't size. `downsize` → cut conviction/size. `proceed` → continue. **These verdicts steer
VIZIER-CHOSEN sizing** (a vague, skill-derived or autonomous amount you picked).

**An explicit named dollar amount is a CONTRACT — here the gate ANNOTATES, it does not gate-out.** When the
user said "buy $N of TICKER" there is no number for the gate to protect (a `cash_amount` market order needs
no price), so an `abstain`/`downsize` becomes an honest caveat on the thesis ("thin data, low confidence")
and you still **execute the user's amount** — the same annotate-don't-prune rule as the read-only count.
The only thing that stops an explicit order is a suspected misparse (the units/typo confirm), never thin
data. Say the data is thin, then comply.

**In read-only research/recommendation mode this gate ANNOTATES, it does not gate-out.** Run it to grade a
name's conviction/quality — an `abstain`/`downsize` becomes an honest caveat on the name ("couldn't fully
verify", low conviction), NOT a reason to drop it from a requested count. Gating a name *out* is an
execution-mode action (it protects money); with no money at risk the user's requested count stands.

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
  - **`slot_base` = the per-asset cap = `NAV * max_pct_per_asset/100`.** Pass that as `slot_base`; the
    core does the rest. A max-conviction (5) position sizes to a configured FRACTION of that cap —
    `conviction_full_size_pct_of_cap` (default 65%) — leaving headroom, while the per-asset cap stays the
    hard ceiling. `size` scales `slot_base` by `conviction/5 × pct/100` and re-caps at the per-asset
    limit. This is the deterministic default; do not invent the number and do NOT apply the fraction by
    hand — the core honors the knob (Rule #2). Retune it per profile in `config/risk_profile.yaml`.
  - **When the user named an exact dollar amount** ("buy $3 of AAPL"), DON'T call `size` — pass that
    amount straight to `limits` and the order. `size`/`allocate` are only for unspecified amounts.
  - **Explicit order below the conviction floor:** `size`/`allocate` silently **drop** a sub-floor leg
    (floor = 2). For a named imperative on a specific ticker, pass **`"explicit_order": true`** so it is
    honored — then flag the low conviction in the output (don't fake enthusiasm).
  - **Mixed request — a user-named leg PLUS skill-derived ideas** ("put $100 into MINE that I named + 2
    of your best picks"). A call-level `"explicit_order": true` floor-exempts EVERY leg, which would wrongly
    keep your sub-floor ideas too. Instead tag only the user-named leg with a per-candidate
    **`"explicit": true`** in its `allocate` candidate row — it bypasses the floor while your sub-floor
    legs in the same call are still dropped honestly. Note the kept named leg is still conviction-weighted
    by ITS OWN conviction, so a low-conviction named pick gets a small slice — if the user clearly wants it
    funded meaningfully, also bump its `weight` (or pass `weighting`).
  - **Equal split / explicit weights.** `allocate` weights by conviction by DEFAULT. When the user asked
    to "split equally across these", pass **`"weighting": "equal"`**; when they gave explicit per-leg
    weights, put a **`"weight"`** on each candidate row (overrides conviction-weighting). Weights are
    **all-or-none** — any `weight` switches the whole call to explicit-weights mode, so every row must
    carry one or none at all (the core rejects a partial set; a weightless leg would silently get $0).
    The per-asset cap still binds in every mode — never hand-weight to dodge it.
- Limits: `python -m vizier limits --json '{"portfolio":{"nav":..,"cash":..,"positions":[...]}, "candidate":{"ticker":..,"value":..,"sector":..}}'`.
  (For a CURRENT over-weight scan, call it with `"value": 0` per held ticker/sector — see the rebalance
  rule in SKILL.md.) A leg that violates max-position/sector/min-cash or collides with max-%/asset on a
  small account (Tension C) is handled by **who chose the size**:
  - **Vizier-chosen size** (vague/skill-derived/autonomous): **downsize/drop the leg** to fit, confirming
    ONCE and explaining the conflict — never breach a limit silently, never freeze the request. To get the
    compliant size, re-run `size`/`allocate` (which apply the cap) rather than back-solving by hand.
  - **An explicit named amount** (the user set the dollars): the cap is a **single caution, not a clamp**.
    Surface the breach once ("$100 across these 2 puts AAA at 31% of NAV, over your 25% per-asset cap — go
    ahead, or keep it within the cap?") and **comply with the answer** — on a yes, re-run `allocate` with
    `"allow_over_cap": true` to deploy the full amount (the `over_cap` legs are disclosed, not shaved).
    Never silently downsize a human's confirmed explicit order. Under **armed autonomy** the cap binds hard
    (no human backstop) — there it is a real ceiling, not a caution.
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
