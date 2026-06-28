# Security Policy

Vizier moves money. Treat it accordingly.

## Never commit real trading state or secrets

Vizier's real runtime state is **private and gitignored** — only `EXAMPLE_*` templates and `.gitkeep` are
committed. The following must **never** reach a public repo:

- `memory/theses/*.yaml` — your real theses (positions, reasons, baselines).
- `memory/decision_log.jsonl` — the audit log of real decisions/fills.
- `memory/nav_snapshots*` — your account NAV series.
- `memory/autonomy_state.json` — the armed-state / day baseline.
- Any `.env` of the Scout or Valet MCPs (API keys, account ids) — those live in their own repos and are
  gitignored there too.

If you fork or clone, double-check `git status` before committing and confirm `git ls-files memory` shows
only `EXAMPLE_*`, `README.md` and `.gitkeep`.

## Safety posture

- Vizier is **paper-first / shadow-mode by default**. Real-money autonomy is the last rung of a deliberate
  ladder (shadow → paper/testnet → live read-only → real money) and is gated.
- The money-safety rules (cumulative + per-run ceilings on a fixed daily baseline, drawdown kill, re-arm
  guard) are **exact, self-latching arithmetic in code** — keep them there, never relocate them into prompt
  text. They are **advisory in the sense that they bind only when the skill calls `autonomy-gate`** each
  candidate with an honest live NAV (Vizier owns no order pipe); the one **hard, executor-enforced dollar
  backstop is the Valet's `MAX_DAILY_VALUE`**, which is why arming autonomy is forbidden until it is set.
  These guards govern **armed, unattended autonomy** — they are not a clamp on a human's confirmed order.
- Always assert the session's `account_type` (`PAPER`/`LIVE`) before an order; trust the account, never the
  config label.

## Reporting a vulnerability

If you find a security issue — especially anything that could let the safety guards be bypassed, or that
could leak private state — please report it privately to the maintainer rather than opening a public issue.
