# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to adhere to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **An exit can no longer be sized larger than the position — the US$-order unit bug (found live, with
  a real US$2 IBKR fill).** IBKR reports the cumulative fill of a **cash-quantity** order — `buy(symbol,
  cash_amount=…)`, *the* documented entry path for equities — **in DOLLARS, not shares**: a real
  `buy(AAPL, cash_amount=2)` returned `filled_quantity = 2.0` while the position actually acquired was
  **0.0063 shares** @ $317.25. The skill's own docs said to read `filled_quantity` post-fill, size the
  protective stop from it, and write it into the thesis as `qty` — so, followed literally, Vizier would
  have placed a **SELL STOP for 2 shares against 0.0063 held** (a ~317x oversell — a naked short) and
  poisoned the tranche guard, the P&L and the scorecard with a dollar figure masquerading as a share
  count. Fixed structurally, not by trusting a corrected field:
  - **New core command `exit-qty`** (`vizier.risk.exit_quantity`) sizes **every** exit — protective stop,
    sell, trim. `position_qty` (from the venue's `positions` tool — the authority; it returned exactly
    `0.0063` for the live fill) is **required and is a hard ceiling**: no combination of inputs can make
    it return more than is held. It refuses outright when no position is resolved (a stop with no
    position is a naked short, not a smaller mistake), rounds DOWN to the lot, treats `filled_quantity`
    as a **cross-check only** — flagging one that exceeds the position, and never trusting one Valet
    marks an estimate (a partial fill of a cash order yields no exact share count) — and points a full
    exit at `close_position`, which resolves the exact fraction itself.
  - **`write-thesis` now REFUSES a `qty` in the wrong unit.** `qty` is validated as a positive share/base
    quantity and cross-checked against `cash_qty`/`entry_price` (`vizier.risk.check_fill_units`): a `qty`
    that equals the cash amount while the price sits far from $1 is dollars-as-shares and is rejected at
    the door, as is any `qty` that cannot be reconciled with the dollars deployed. The USD belongs in the
    existing `cash_qty` field — the dollar figure's only legitimate home.
  - **`trim-qty` now requires `current_qty`** in both modes (it was optional in dollar mode, where an
    unbounded `dollars / price` could size an exit larger than the position). The holding is the ceiling
    that makes an oversell impossible, so it is mandatory rather than merely recommended.
  - **`scorecard` refuses to score a thesis whose `qty` is not a usable share quantity.** A record
    written before this guard (or hand-edited) could hold dollars in `qty`, and `entry_price * qty` would
    report a P&L off by the share price. It is now NAMED in `skipped` with the reason instead — the
    scorecard's whole value is that its verdict is trustworthy, so a fabricated number is worse than none.
  - **Docs rewritten wherever the old instruction lived** — `SKILL.md` (safety rules + memory discipline
    + core command table), `references/execution-mechanics.md` (new **Units** table; the IBKR order flow's
    post-fill step), `references/pipeline.md`, `references/anchor-example.md`, `memory/theses/
    EXAMPLE_thesis.yaml`, `README.md`. Units are now stated explicitly everywhere a quantity appears
    (`qty`/`quantity`/`position_qty` = shares/base units; `cash_amount`/`cash_qty`/`filled_cash` = USD) —
    the whole class of bug here is a number whose unit is implicit.
  - **Venue accuracy:** this is **IBKR-specific**. The crypto/CCXT side reports `filled` in base units
    already and was never broken — the docs now say so explicitly, so nobody "fixes" it into existence
    there. The `positions`-first exit discipline is identical on both venues regardless.

## [0.4.0] - 2026-07-11

### Added
- **`reduce-thesis-qty` — partial-sell bookkeeping (closes the phantom-balance hole).** There was no
  way to decrement a thesis `qty` after an executed trim, so the tranche guard kept approving sells
  against the pre-trim quantity — a balance the account no longer held. The new command
  (`{"ticker", "open_date", "qty_sold"}`, optional `horizon_tag`, `--commit` like the other writers)
  decrements the stored `qty` and refuses to go below zero. It deliberately never closes the thesis:
  a qty that reaches 0 returns a note pointing to `close-thesis`, which owns `exit_price`/
  `realized_pnl`. SKILL.md's memory discipline and `references/execution-mechanics.md` now instruct
  running it right after any trim fill.
- **`write-thesis` gained `overwrite` (deliberate update) and the readers gained `horizon_tag`
  (same-day-lot disambiguation)** — see the Fixed entry below for why.

### Fixed
- **`write_thesis` no longer silently clobbers a same-day lot of the same ticker.** The store keyed
  theses on the fixed `{ticker}-{open_date}.yaml` path, so opening a `core` and a `tactical` lot of one
  ticker on the same day (the §D tranche design's own legitimate case) left ONE file — the second write
  erased the first, and tranche accounting then approved sells against a balance missing the erased lot.
  A write now never lands on an existing file: a new lot gets a free name (base, then `-{horizon_tag}`,
  then a numeric suffix), and a deliberate update requires `overwrite: true`. `read-thesis`/
  `close-thesis`/`reduce-thesis-qty`/`update-reviewed` locate lots by glob (old-format files keep
  working) and, when one ticker+date holds several lots, an ambiguous call errors loudly asking for
  `horizon_tag` instead of guessing which lot to touch.
- **`drawdown` no longer crashes on a naive NAV timestamp.** `compute_drawdown` parsed timestamps with
  raw `fromisoformat`, so one snapshot written without a timezone made the aware-vs-naive comparison
  TypeError at sort time — killing the circuit breaker's whole drawdown leg over a formatting detail.
  It now normalizes through the module's own `_parse_dt` (naive = UTC).
- **`scorecard` annotates a benchmark series truncated at the END of the window.** The coverage check
  only protected the start: a benchmark ending weeks before the thesis window's end silently backfilled
  the last close and compared unequal windows. Consistent with the annotate-don't-suppress rule, the
  alpha is kept and `benchmark_note` now names the gap when the series ends more than ~5 days short.
- **`references/execution-mechanics.md` caught up with Valet v0.6.0.** The venue cheatsheet said IBKR
  19 tools / crypto 14 with `stop_order` ABSENT — while its own stop guidance (correctly) told you to
  prefer the crypto-native `stop_order`. Now: IBKR **20** tools (adds `reconcile_pending`), crypto
  **16** (adds `stop_order` + `reconcile_pending`), and the absent list is the real 4
  (`preview_order`, `trailing_stop`, `bracket_order`, `wait_for_fill`).
- **`references/output-template.md` performance section pointed at the pre-scorecard world** ("an
  aggregator is on the roadmap; until it lands, read the closed records by hand"). It now routes "how
  am I doing?" through the deterministic `scorecard` command, matching SKILL.md.
- **`references/anchor-example.md` modeled the wrong explicit-order pattern.** The canonical trace
  passed the call-level `"explicit_order": true` to `allocate` for a 100% skill-derived slate — exactly
  what SKILL.md warns floor-exempts Vizier's own ideas — and told the reader a gate may "drop the leg
  even under the explicit order" without saying why that isn't a contract breach. The trace now keeps
  the flag off (the $100 total and the count are the contract; the names are skill-derived, so the
  floor applies and the shortlist backfills) and explains the annotate-vs-prune boundary in place.

### Changed
- **Journal-per-leg is now the rule for ANY multi-leg batch, confirmation mode included.** The
  discipline existed only in the autonomy loop, while the canonical confirmation trace journaled all
  three legs at the end — a crash mid-batch would leave confirmed fills with no journal (phantom
  positions). SKILL.md states the general rule, and the anchor trace journals each leg
  (`append-decision` + `write-thesis`) before sending the next.
- **The Valet hard stop got an explicit scope** (SKILL.md): a rejection/SafetyError halts the
  REMAINING legs; report what filled and what didn't; never unwind an already-filled leg on your own.
- **SKILL.md no longer hardcodes the maintainer's name** — the skill is public and installable by
  anyone, so "the sovereign" is simply the user.
- **README refresh:** live GitHub Actions CI badge, real test count, updated sibling-repo tool counts
  (worded not to re-age), a "Measuring edge — the scorecard" section, and a collapsible example of a
  `/vizier` session output (labeled illustrative/paper).

## [0.3.0] - 2026-07-01

### Added
- **`scorecard` — the deterministic performance verdict ("does the brain have edge?").** New core
  module + CLI command scoring EVERY thesis (open + closed): per-thesis P&L and return, days held,
  and **benchmark-relative alpha** (same-window SPY for ibkr / BTC-USDT for crypto — the skill passes
  Scout's `price_history` bars in; the core does the date alignment and arithmetic). Aggregates: hit
  rate, realized/unrealized P&L, win/loss profile, avg alpha — overall, per horizon tag and per venue —
  plus an activity summary from the decision log. Honesty rules throughout: unscorable theses are
  NAMED in `skipped` (never guessed), alpha is null with the reason when the benchmark window isn't
  covered, empty aggregates are null (not fake zeros), and returns are period returns (no
  annualization). This is what turns the paper-first ladder from anecdotes into evidence.
- **The memory repo now PUSHES on commit (backup of the track record).** `commit_memory` pushes after
  each commit when a private remote is attached (best-effort: a network failure only skips the backup,
  never breaks a trading flow; no remote = no-op). The theses/decision-log/NAV series are the
  scorecard's raw material — a disk failure must not erase the project's evidence. SKILL.md now
  instructs passing `--commit` on memory writes.

### Fixed
- **`drawdown` no longer mixes venues in one NAV series** (manager code-review finding). NAV snapshots
  record a `venue`, but `compute_drawdown` aggregated ALL snapshots regardless — an interleaved
  IBKR($8)/crypto($1000) history reads as a phantom ~99% drawdown and would trip the circuit breaker
  on garbage (SKILL.md itself forbids mixing NAVs under one denominator). `drawdown` now takes `venue`;
  a multi-venue series without the filter is refused loudly instead of fabricating a number.

### Changed
- **Crypto protective stops: exchange-native first (Valet ≥0.6.0), soft only as fallback.** The skill
  now places the crypto Valet's new `stop_order` (a trigger order resting ON the exchange — fires with
  no agent running) as the default protection, with the stop-LIMIT gap risk disclosed; the soft
  skill-monitored stop and its verbatim disclosure remain only for venues without native stop support.
  Updated across SKILL.md, `references/execution-mechanics.md`, `anchor-example.md` and
  `output-template.md`.

## [0.2.6] - 2026-07-01

### Fixed
- **`allocate` rejects a PARTIAL set of per-candidate weights** (manager code-review finding). Any
  `weight` switches the whole call to explicit-weights mode, where a leg WITHOUT one defaulted to 0
  and silently received $0 — the same silent-unfaithfulness class as the degenerate-basis $0 deploy
  fixed in 0.2.1 (and the `weight_fallback` recovery only fires when ALL weights sum to zero, so the
  mixed case slipped through). Weights are now all-or-none: give every leg a `weight` or none at
  all; a partial set raises loudly instead of quietly starving the unweighted legs. SKILL.md and
  `references/pipeline.md` state the rule where the mixed-`allocate` guidance lives.

## [0.2.5] - 2026-06-30

### Fixed
- **The no-co-batch rule now explicitly names `WebSearch`/`WebFetch` (and subagents) as gated.** A
  real GOLD11 session hung ~25 min because the agent co-batched a `WebSearch` with the always-allow
  Scout reads — the exact freeze the 0.2.4 rule warns about, but its examples only listed the core
  Bash call and Valet, so the agent mentally grouped `WebSearch` with "reads" (like Scout) instead of
  with gated tools. The rule now makes the trap explicit: the ONLY auto-approved tool is `mcp__scout`;
  everything else is gated *including tools that feel like a read* (`WebSearch`/`WebFetch`), and
  `WebSearch` is called out as the classic trap. Fire any gated tool in its own step. (Paired with a
  machine-local change: `WebSearch`/`WebFetch` were added to always-allow, which removes the freeze at
  the source on this install; the rule keeps the defense portable and for any other gated tool.)

## [0.2.4] - 2026-06-30

### Fixed (hardening from a user-sim round on the 0.2.3 hang fix)
- **The "don't co-batch" rule is generalized to ANY gated tool, not just the core.** 0.2.3 only
  forbade batching `python -m vizier` with Scout calls, but the freeze is caused by *any* gated tool
  (the core Bash, AND Valet `ibkr`/`crypto`) sharing a parallel block with the always-allow Scout
  calls — and Stage 0a / the class-1 sweep / the session-start diff actively mix Valet reads with the
  Scout regime read. The rule now says: sequence by approval class — always-allow Scout in one block,
  gated calls (core, Valet) separately.
- **The core interpreter now has a concrete discovery path.** 0.2.3 said "use `<skill_dir>/.venv`"
  but never told the agent how to obtain that absolute path from an arbitrary cwd. It now points at
  the standard `~/.claude/skills/vizier/.venv/…` first, with a Glob fallback, and says to cache it.
- **Every `references/*.md` now carries a one-line note** that `python -m vizier` means the resolved
  venv interpreter (the bare commands in fenced examples were copy-verbatim traps to the system python).
- **The session-start `positions`-vs-`list-theses` diff + `provenance` step is now explicitly
  best-effort too** (it shares the core/Valet dependency with the thesis-check), so a fresh machine
  degrades with a note instead of blocking the read-only report.
- **`reconcile` mislabeled as a Valet tool** in the Stage 0a book-pull is corrected to
  `reconcile_pending` (Valet's actual tool; `reconcile` is a core CLI command).

### Added
- **The session's new Scout tools are now routed in the pipeline coverage areas** (`references/pipeline.md`):
  `fda_events`, `btc_network`, `defi_fees`, `coinbase_premium`, `stablecoin_peg`, `cot_positioning`,
  `commodity_ratios`, and the relative-value/pairs primitives `cointegration_test` /
  `find_cointegrated_pairs` — previously unreachable by the brain.

## [0.2.3] - 2026-06-30

### Fixed
- **The core interpreter is now pinned to the skill's own venv, not bare `python`.** A real session
  hung and then failed because the skill told the agent to run `python -m vizier …`, which in a fresh
  chat resolves to the **system** Python — where the core isn't installed (`No module named vizier`).
  SKILL.md now requires resolving the interpreter once (`<skill_dir>/.venv/Scripts/python.exe` /
  `bin/python`) and using it for every core call, with a create-if-missing recipe.
- **Never batch a `python -m vizier` Bash call with Scout MCP calls in one parallel tool block.** A
  pending permission on the core call freezes the whole batch — including the auto-approved Scout
  calls — with no timeout (the same session sat ~25 min looking like "thinking forever"). SKILL.md now
  forbids co-batching and tells you to sequence data and core calls.
- **Session-start thesis-check is now explicitly best-effort.** If the core can't be resolved it notes
  "thesis-check unavailable" once and continues the read-only work instead of blocking the report;
  `list-theses` reads `memory/theses/*.yaml`, which can be read directly as a fallback.

## [0.2.2] - 2026-06-28

### Fixed
- **`allocate` now rejects a non-finite `nav`** (`inf`/`nan`) alongside the existing `nav <= 0` guard,
  for symmetry with the finiteness checks added to `total_amount`/`weight`/`conviction` in 0.2.1. A
  non-finite NAV would otherwise yield a non-finite per-asset cap and silently disable the cap signal.

## [0.2.1] - 2026-06-28

### Fixed
- **`allocate` honored an explicit positive budget faithfully even when the split basis is degenerate.**
  Found in adversarial QA: all-zero convictions (or all-zero explicit weights) made `weight_sum == 0`,
  so every leg sized to `0` and the whole amount landed in `unallocated` with `ok:true` — a silent
  contract break (deployed total ≠ the amount the user asked for). It now falls back to an even split
  and flags `weight_fallback: true` so the recovery is never silent. Non-finite `total_amount`/`weight`/
  `conviction` (NaN/inf) are now rejected up front alongside the existing negative-value guards.

## [0.2.0] - 2026-06-28

Everything that landed since the first build, plus a deliberate sharpening of the product's posture from
"careful" toward **faithful and powerful** — obedient to explicit intent, with judgment (not hard caps) as
the overtrading brake.

### Manager / breadth-discovery mode (the big addition)
- A broad request ("analyze the market and bring me recommendations" / "find the best opportunities") now
  enters **manager mode**: partition the market into coverage areas, fan out **research-only** `vizier-
  research-envoy` subagents in parallel, dedup, then funnel-prune by potential, risk/reward and
  **correlation-based diversification** so the slate isn't three of the same bet. Read-only by default;
  only the main thread ever executes. Full Stage B spec in `references/pipeline.md`; report shape in
  `references/output-template.md`. Hard research firewall via the bundled envoy agent type.

### Faithful-execution posture (this release's behavioral spine)
- **New `SKILL.md` "Posture" section** encoding: explicit intent executes exactly (no silent downsizing,
  no nagging, no re-litigating); a genuinely risky move earns at most ONE caution, then comply; the only
  other confirm is a suspected **units/typo misparse** (check understanding, not morality); the overtrading
  defense is **portfolio-aware judgment, not a "max N trades" cap**; and **safety scales with autonomy**
  (limits are a single caution to a human at the wheel, binding ceilings only once autonomy is armed).
- **`allocate` gained `allow_over_cap`** (`vizier/risk.py` + CLI): an explicit, confirmed dollar amount is a
  **contract** — when the per-asset cap would shave a multi-name budget on a small account, the skill warns
  once and (on a yes) re-runs with `allow_over_cap=true` to deploy the **full** amount, disclosing each
  `over_cap` leg instead of silently leaving money `unallocated`. The cap still binds Vizier's own sizing
  and armed-autonomy. Each allocation now also reports an `over_cap` risk flag.
- **Data-sufficiency on an explicit order ANNOTATES, it no longer downsizes/refuses it** — a `cash_amount`
  order needs no price, so thin data becomes an honest caveat, not a block (the same annotate-don't-prune
  rule already used for read-only recommendation counts). Verdicts still steer Vizier-chosen sizing.
- **Portfolio-aware brake made a REQUIRED step** (`references/pipeline.md` Stage 0a): any autonomous, vague
  or manager flow must pull the live book first and reason "is this additive, or is doing nothing right?"
- **Circuit breaker reframed**: a single caution-then-comply on the confirmation path; the abandon-and-
  disarm behavior is scoped to armed, unattended autonomy (robot-malfunction backstop).

### Allocate & intent hardening (pre-posture)
- `allocate` per-candidate `explicit` flag (keep a user-named sub-floor leg while dropping skill-derived
  ones) and `weighting: conviction | equal` + per-leg `weight`; input validation hardened.
- Honor an explicit **recommendation count** over the depth-funnel cap; honor an explicit **read-only
  intent** over execution-mode defaults; MCP-contract edges hardened against the live Scout/Valet servers.

### Doc-accuracy
- Corrected the safety-model framing across `README.md`, `SECURITY.md` and `references/autonomy-and-
  safety.md`: Vizier's ceilings + drawdown-kill are exact, self-latching **arithmetic that binds only when
  the skill calls `autonomy-gate`** with an honest NAV (Vizier owns no order pipe); the one **hard,
  executor-enforced dollar backstop is the Valet's `MAX_DAILY_VALUE`**. The drawdown-kill **latches in
  code** but the disarm is the skill obeying the block — the gate does not auto-disarm.

### Tests
- 117 offline, deterministic tests (was 96 at 0.1.0); added coverage for `allow_over_cap` (full-amount
  honoring + the default-still-shaves guard).

## [0.1.0] - 2026-06-27

First build of Vizier — the decision-making brain of the Scout/Valet/Vizier trio.

### The skill (judgment + orchestration)
- `SKILL.md`: one natural-language-driven `/vizier` command with intent classification (read-only sweep,
  research, portfolio health, research+execution, thinking-out-loud), multi-horizon mandate (`core` vs
  `tactical`), venue routing (`ibkr` / `crypto`), confirmation-by-default with opt-in autonomy, and the
  non-negotiable safety rules.
- `references/`: progressive-disclosure detail — execution mechanics (the per-venue cheatsheet), the
  subagent pipeline (analysts → bull×bear → trader → gates → pre-mortem), the output template, the
  autonomy/safety discipline, and an end-to-end anchor example.

### The deterministic core (money-sensitive math + state)
- **Risk & sizing** (`vizier/risk.py`): conviction sizing, budget allocation, portfolio limits, circuit
  breaker, the cumulative-daily and per-run ceilings, drawdown kill, and `trim-qty` (%/$ → sell quantity,
  rounding down).
- **Memory** (`vizier/memory.py`): the thesis store (with the quantitative `baseline_snapshot` and a
  required filled `qty`), append-only decision log, NAV snapshots + drawdown, tranche accounting by horizon
  tag, the armed-state day baseline, and `build-own-sent-orders` (shapes the log for reconciliation).
- **Autonomy gate** (`vizier/autonomy.py`): one composed per-candidate verdict (not-armed · per-run ·
  cumulative · drawdown-kill), refusing without a `begin-run` marker.
- **Re-arm guard**: `arm-autonomy` refuses to re-arm mid-window (the drain vector); re-arm requires an
  explicit disarm or window expiry.
- **Reconciliation & provenance** (`vizier/reconcile.py`): double-buy protection against the own-order log ∪
  positions (venue-aware lag window), and unknown-provenance flagging.
- **Data-sufficiency gate** (`vizier/data_sufficiency.py`): a minimum-evidence check per decision type that
  downsizes or abstains on thin data, even under an explicit order.
- **CLI** (`vizier/cli.py`): every helper exposed as `python -m vizier <command> --json '...'` returning an
  `{ok, data}` envelope.

### Safety & privacy
- Born paper-first / shadow-mode; the §B safety guards are code-enforced and fail closed.
- Code is public; real trading state under `memory/` is gitignored (only `EXAMPLE_*` templates committed).

### Tests
- 96 offline, deterministic tests covering the core and the adversarial §B scenarios.

[0.2.0]: https://github.com/pedrobraiti/vizier-trading-skill
[0.1.0]: https://github.com/pedrobraiti
