# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to adhere to
[Semantic Versioning](https://semver.org/).

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
