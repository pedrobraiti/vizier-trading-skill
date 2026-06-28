"""Vizier — deterministic safety/state core for the /vizier Claude Code skill.

The skill (SKILL.md) does the LLM orchestration; this package holds the
money-sensitive logic that must be exact and must not be forgotten between
rounds: risk limits, conviction sizing, the B autonomy ceilings, the thesis
store and decision log, exposure reconciliation and the data-sufficiency gate.
"""

from __future__ import annotations

__version__ = "0.2.2"
