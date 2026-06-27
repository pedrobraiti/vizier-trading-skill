# Output format — the §F template

TL;DR-first, then organized detail. The order below is the default, **not a cage** — add bonus sections
when there's something worth saying, and give more than the minimum when it helps. Lead with the horizon
the user asked about.

```
TL;DR — <the decision in ONE line>

By horizon
  • Long  — <quality/fundamentals read>
  • Short — <technicals/catalyst/flow read>
  (mark divergence explicitly, e.g. "long: strong; short: overbought — wait")

Action — <the trade(s): venue, side, size, order type> OR "none — no edge today"

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

- **Open with crossed thesis triggers.** If the start-of-session thesis-check found an open thesis that
  crossed its review trigger, that goes at the **TOP**, above everything — never bury it.
- **Honest price-target scope.** Relay the sell-side consensus as third-party data **plus** at most a
  crude DCF/multiple you flag loudly as crude. Do **not** present an independent valuation as if Scout
  supported it — `valuation_history` does not exist.
- **Signal low conviction honestly.** Under an explicit order with weak edge, execute (if it clears the
  gates) **and** say the conviction is low — never fake enthusiasm.
- **Crypto protection caveat.** Any crypto position with a stop must carry the line: *"protection is
  skill-managed (monitoring), not a resting stop on the exchange"* — and, when honest, that in
  confirmation mode it is only checked when you next run Vizier / at session start, not continuously (a
  true 24/7 soft stop needs armed autonomy + a scheduled loop).
- **Abstention is a valid Action.** "None — no edge today" is a complete, respectable answer.
- **Read-only calls never show an Action that executed** — they end at recommendations.

## Empty-call (market sweep) shape

Lead with portfolio state + what moved today + open theses needing attention + any fired alerts, all
**read-only**. Aggregate the book's horizon by weighting per position size (conviction as the
tie-breaker). When not logged into IBKR, degrade to a market-only panorama and say positions are
unavailable without a session.

## Performance summary (periodic, folded into the morning brief when enabled)

Return vs SPY, P&L per thesis, hit rate. This is how the user answers "how am I doing?" — content fixed,
exact cadence/format left to the moment. The numbers come from **closed** theses: `list-theses` returns
only OPEN ones, so read the closed records from `memory/theses/*.yaml` with `status: closed` and use
their stored `realized_pnl` / `alpha_vs_spy` (which were computed at close, not re-derived now). Do
**not** free-hand P&L/alpha math in the output. (A deterministic closed-thesis aggregator — hit rate,
total P&L, alpha — is the cleaner home for this and is on the roadmap; until it lands, read the closed
records and present their stored fields rather than recomputing.)
