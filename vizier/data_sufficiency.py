"""Data-sufficiency gate (spec, §B).

The Scout can return ``{ok: true}`` with null fields when a source is
rate-limited — to the skill that looks like "good data". This gate sits between
the analysts and the risk gate: it defines a MINIMUM evidence set per decision
type and returns a verdict (``proceed`` / ``downsize`` / ``abstain``).

This function is a pure ANNOTATOR — it grades evidence, it does not execute
anything. How the skill ACTS on the verdict depends on who chose the size:
  * VIZIER-chosen sizing (a vague, skill-derived or autonomous amount) → the
    verdict steers it: thin data downsizes or abstains ("I cannot size this
    responsibly, the data isn't there"). Abstaining is honest, not a failure.
  * An EXPLICIT named dollar amount (or a read-only recommendation count) is a
    CONTRACT — there the verdict only ANNOTATES (an honest "thin data" caveat)
    and the skill still honors the user's amount/count. A ``cash_amount`` market
    order needs no price, so there is nothing for the gate to gate-out; only a
    suspected misparse stops it, never thin data (spec, §10 + the faithful-
    execution posture in SKILL.md).

Expected input shape: ``scout_responses`` is a flat dict of signal name → value
that the skill has already extracted from the Scout envelopes (so a null field
shows up as ``None``, an empty series as ``[]``). The keys below are the signal
names this gate looks for.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Recommended actions, ordered by severity.
PROCEED = "proceed"
DOWNSIZE = "downsize"
ABSTAIN = "abstain"

# Signal names that count as a valuation "multiple" — any one present satisfies
# the "≥1 multiple" requirement.
MULTIPLE_KEYS = ("pe", "pe_ratio", "forward_pe", "ev_ebitda", "pb", "ps", "ev_sales", "peg")


@dataclass(frozen=True)
class Requirement:
    """One piece of evidence a decision needs.

    ``critical`` requirements are the anchor of the decision: missing one means
    we cannot even begin (→ abstain). Missing a non-critical requirement means
    we have the anchor but incomplete support (→ downsize).
    """

    name: str
    critical: bool
    check: Callable[[dict[str, Any]], bool]


def _present(responses: dict[str, Any], key: str) -> bool:
    return responses.get(key) is not None


def _non_empty(responses: dict[str, Any], key: str) -> bool:
    value = responses.get(key)
    return value is not None and len(value) > 0


def _has_any_multiple(responses: dict[str, Any]) -> bool:
    return any(_present(responses, key) for key in MULTIPLE_KEYS)


# Minimum evidence set per decision type. Extend here as new decision types
# appear — the gate stays declarative.
REQUIREMENTS: dict[str, list[Requirement]] = {
    "valuation": [
        Requirement("price", critical=True, check=lambda r: _present(r, "price")),
        Requirement("multiple", critical=False, check=_has_any_multiple),
        Requirement("sector", critical=False, check=lambda r: _present(r, "sector")),
    ],
    "technical": [
        Requirement(
            "price_history", critical=True, check=lambda r: _non_empty(r, "price_history")
        ),
    ],
    "macro": [
        Requirement("macro_context", critical=True, check=lambda r: _present(r, "macro_context")),
    ],
    "news_sentiment": [
        Requirement("news", critical=True, check=lambda r: _non_empty(r, "news")),
    ],
}


def assess(scout_responses: dict[str, Any], decision_type: str) -> dict[str, Any]:
    """Decide whether the evidence supports a decision of ``decision_type``.

    Returns ``{sufficient, missing, recommended_action, decision_type}``.

      * all requirements met            → sufficient, proceed
      * a critical requirement missing  → abstain (no anchor to reason from)
      * only non-critical missing       → downsize (anchor present, support thin)

    An unknown ``decision_type`` is treated conservatively as abstain: the gate
    will not green-light a decision whose evidence it cannot characterize.
    """
    requirements = REQUIREMENTS.get(decision_type)
    if requirements is None:
        return {
            "sufficient": False,
            "missing": [f"unknown decision_type: {decision_type}"],
            "recommended_action": ABSTAIN,
            "decision_type": decision_type,
        }

    missing = [req.name for req in requirements if not req.check(scout_responses)]
    missing_critical = [
        req.name for req in requirements if req.critical and not req.check(scout_responses)
    ]

    if not missing:
        action = PROCEED
    elif missing_critical:
        action = ABSTAIN
    else:
        action = DOWNSIZE

    return {
        "sufficient": not missing,
        "missing": missing,
        "missing_critical": missing_critical,
        "recommended_action": action,
        "decision_type": decision_type,
    }
