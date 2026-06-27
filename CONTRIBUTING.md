# Contributing to Vizier

Thanks for your interest. Vizier is the **brain** of the Scout/Valet/Vizier trio — a Claude Code skill plus
a deterministic Python core. A few principles keep it safe and coherent.

## The inviolable boundary (read this first)

Vizier **consumes** the Scout and Valet MCP servers exactly as they are. It **never** modifies, adds to, or
plugs logic into them. If something is missing, solve it **in the skill** — a prompt, or a helper in the
deterministic core — never by changing an MCP. PRs that push trading logic into Scout or Valet will be
declined.

## Where logic lives

- **Money-sensitive math and state** (sizing, ceilings, drawdown kill, reconciliation, the thesis/decision
  store) live in the **deterministic Python core** (`vizier/`), as pure, testable functions. They must not
  be re-derived in skill prose — the whole point of the hybrid is that code can't forget between rounds.
- **Judgment and orchestration** (intent classification, the analyst pipeline, output format) live in
  `SKILL.md` and `references/`.

If a change touches the money-safety guarantees, it belongs in `vizier/` **with tests**, not in a prompt.

## Development

```bash
python -m venv .venv && pip install -e ".[dev]"
pytest -q          # all tests must pass (deterministic, offline, no real git)
ruff check .       # must be clean
```

- **Tests are part of the deliverable.** Add behavioral tests for new core logic — especially adversarial
  ones for anything in the §B safety path. Inject the clock (`now=`) and the memory dir (`memory_dir=`) so
  tests stay deterministic and never touch a real repo or the network.
- Keep `ruff check .` clean (line length 100; rules in `pyproject.toml`).
- Never hardcode magic numbers in the money-math — use named constants (`vizier/constants.py`) or the
  editable `config/risk_profile.yaml`.

## Privacy

Never commit real trading state. Only `EXAMPLE_*` templates and `.gitkeep` belong under `memory/`; theses,
the decision log, NAV snapshots and autonomy state are gitignored. See [SECURITY.md](SECURITY.md).

## Commits & PRs

- Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `refactor:`,
  `test:`, `chore:`).
- Keep subjects in the imperative and explain the **why** when it isn't obvious.
- Run `pytest -q` and `ruff check .` before opening a PR.
