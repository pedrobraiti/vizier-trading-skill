# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to adhere to
[Semantic Versioning](https://semver.org/).

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

[0.1.0]: https://github.com/pedrobraiti
