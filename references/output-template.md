# Output format — the §F template

TL;DR-first, then organized detail. The order below is the default, **not a cage** — add bonus sections
when there's something worth saying, and give more than the minimum when it helps. Lead with the horizon
the user asked about.

```
MODE: PAPER | LIVE · venue: ibkr | crypto    ← REQUIRED on any confirm/execute output

TL;DR — <the decision in ONE line>

By horizon
  • Long  — <quality/fundamentals read>
  • Short — <technicals/catalyst/flow read>
  (mark divergence explicitly, e.g. "long: strong; short: overbought — wait")

Action — <the trade(s): venue, side, size, order type> OR "none — no edge today"
  per name, tag the status:  [RECOMMENDATION — not placed]  |  [EXECUTED — order id …]

Conviction — n/5  ·  <one line why>

Scenarios + price target
  • Bear  <…>   • Base  <…>   • Bull  <…>
  Price target: <analyst-consensus relay (via analyst_view) + a crude, EXPLICITLY-caveated
  DCF/multiple>. NO independent valuation — there is no valuation_history; say so.

Risks & caveats — <the real ones, including data gaps from the sufficiency gate>

Pre-mortem — <why this trade NOW could be a mistake (timing/execution)>

Post-trade portfolio — <% exposure per name / per sector / cash, after the trade>

[free bonus — anything important the standard sections didn't capture]

Sources — <primary first (SEC, releases), then aggregators; with as_of where relevant>
```

## Rules that bind the format

- **MODE/venue banner is REQUIRED, not prose.** Every output that confirms or executes an order must
  open with the `MODE: PAPER | LIVE · venue: ibkr | crypto` banner line, sourced **directly from the
  `session_status` `account_type` assertion** you ran before the order (PAPER/LIVE is the one field
  identically named on both venues). Name every venue in play. It is a mandatory field of the template —
  the user must never have to guess whether real money is at stake or which exchange it lands on. A
  mixed-venue slate shows both venues (e.g. `venue: ibkr + crypto`).
- **Per-name status tag — advice vs order.** Each survivor's Action line carries an explicit tag so a
  recommendation is never mistaken for a placed order: **`[RECOMMENDATION — not placed]`** for anything
  not executed (research mode, abstained leg, awaiting confirmation), **`[EXECUTED — order id <id>]`**
  once the fill is confirmed. Never rely on prose to convey whether an order went in.
- **Unsolicited crypto is called out at confirm.** When the user did NOT mention crypto but the slate
  carries a crypto leg, the confirmation step must flag it EXPLICITLY before any crypto order — name it,
  its venue and its separate-account/stop nature ("1 of 3 is BTC/USDT on the `crypto` exchange —
  separate account; the protective stop is a stop-LIMIT resting on the exchange — include it?") — and
  the crypto venue must be okayed. Do not bury an unsolicited crypto leg inside the slate (SKILL.md
  safety rules).
- **Open with crossed thesis triggers.** If the start-of-session thesis-check found an open thesis that
  crossed its review trigger, that goes at the **TOP**, above everything — never bury it.
- **Honest price-target scope.** Relay the sell-side consensus as third-party data **plus** at most a
  crude DCF/multiple you flag loudly as crude. Do **not** present an independent valuation as if Scout
  supported it — `valuation_history` does not exist.
- **Signal low conviction honestly.** Under an explicit order with weak edge, execute (if it clears the
  gates) **and** say the conviction is low — never fake enthusiasm.
- **Flag corporate-event blind spots.** Scout's free news feed (GDELT) is not exhaustive and can miss
  capital-structure events (raises, dilution, buybacks, M&A). On any execution-grade thesis, state in
  **Risks & caveats** that the news feed may be incomplete for corporate events, and note the dedicated
  recent-filings / capital-structure check you ran (or, if you couldn't run it, that the gap is open).
- **Crypto protection caveat — say which KIND of stop it is.** With the exchange-native `stop_order`
  placed: state it rests on the exchange and fires unattended, plus the stop-LIMIT gap risk ("a violent
  gap can jump the limit"). On the soft fallback (venue without native stops) the old line is mandatory
  verbatim: *"protection is skill-managed (monitoring), not a resting stop on the exchange"* — and that
  in confirmation mode it is only checked when you next run Vizier / at session start, not continuously
  (a true 24/7 soft stop needs armed autonomy + a scheduled loop).
- **Abstention is a valid Action.** "None — no edge today" is a complete, respectable answer. This holds
  even when the data-sufficiency gate abstains/downsizes **every** candidate — dropping the whole slate is
  a legitimate outcome, not a failure to act. Report "None — no edge today" honestly; don't force a trade
  to look productive. (**Caveat — read-only with an explicit count:** there the gate ANNOTATES, it does not
  gate-out — deliver the N names asked for with honest caveats rather than dropping them; see the breadth-
  discovery shape below.)
- **Read-only calls never show an Action that executed** — they end at recommendations.

## Empty-call (market sweep) shape

Lead with portfolio state + what moved today + open theses needing attention + any fired alerts, all
**read-only**. Aggregate the book's horizon by weighting per position size (conviction as the
tie-breaker). When not logged into IBKR, degrade to a market-only panorama and say positions are
unavailable without a session.

## Breadth-discovery report shape (class 2 — the manager's slate)

When you ran the Stage B fan-out, **lead with this report**, then give the standard §F per-name block
for each survivor that finished the depth pipeline. The **pruning rationale is the centerpiece** — "who
survived and why", as if a superior will judge it.

```
TL;DR — <N ideas across M areas; top pick X; "execute?" only if the request authorized it>

Macro framing — <regime line: rates / VIX / dominance, and what it favors> (2-3 lines)

Coverage map — who looked where
  • {Area 1} envoy — {scope} — surfaced: A, B
  • {Area 2} envoy — {scope} — surfaced: C
  • {Area 3} envoy — {scope} — no edge (reported empty — this is honest, not a gap)
  ... (one line per envoy, including the empty-handed ones)

Candidate slate — the FULL funnel input (pre-prune)
  | ticker | area | venue | case | key risk | conv | evidence (as_of) |
  (every row the envoys returned, so the pruning can be judged)

Pruning rationale — WHO SURVIVED AND WHY   ← the centerpiece
  • Kept:  X (area, conv) — best risk/reward, lowest correlation to the rest
           Z (area, conv) — diversifier, near-term catalyst
  • Cut:   Y — corr {0.x} with X (redundant bet; kept the stronger)
           W — weaker risk/reward
           V — evidence too thin (would fail data-sufficiency anyway)
  • Diversification check — correlation_matrix (equity) / crypto_correlation_matrix (crypto)
    summary; cross-venue risk-on/off note (qualitative — there is no cross-venue matrix)

The final ≥3 (handed to the deep-dive pipeline)
  • X · Z · … — each: venue + one-line + first-pass conviction

Venue note — equity legs sized vs IBKR NAV; crypto legs vs crypto NAV; never mixed.
```

Then, for each survivor, the standard §F block above (By horizon · Action · Conviction · Scenarios ·
Risks · Pre-mortem · Post-trade portfolio · Sources). Abstention still applies to an **unscoped / open**
request — if the funnel and gates drop everything, "**None — no edge today**" is a complete, respectable
slate. But when the user gave an **explicit count in read-only mode**, the contract is **N names
delivered-with-flags**: thin / low-conviction names are shown with an honest caveat ("couldn't fully
verify", weak edge) rather than silently removed. If truly nothing researchable exists for a name, say so
per name — still listing it — rather than coming in under the requested N.

## Performance summary (periodic, folded into the morning brief when enabled)

Return vs SPY, P&L per thesis, hit rate. This is how the user answers "how am I doing?" — content fixed,
exact cadence/format left to the moment. The numbers come from **closed** theses: `list-theses` returns
only OPEN ones, so read the closed records from `memory/theses/*.yaml` with `status: closed` and use
their stored `realized_pnl` / `alpha_vs_spy` (which were computed at close, not re-derived now). Do
**not** free-hand P&L/alpha math in the output. (A deterministic closed-thesis aggregator — hit rate,
total P&L, alpha — is the cleaner home for this and is on the roadmap; until it lands, read the closed
records and present their stored fields rather than recomputing.)
