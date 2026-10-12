"""Numeric input validation shared by every money-sensitive guard.

A guard is only as safe as the numbers it is fed. ``NaN`` compares False against
everything, so ``nan <= 0`` lets a NaN NAV straight past a "must be positive"
check and every limit built on it reads "allowed"; a negative ``slot_base``
produces a negative (short) size. These helpers turn every such input into a
clear ``ValueError`` naming the field, so a guard can never answer with a
number computed from garbage.

Numbers arrive from JSON (``int``/``float``) and, via the Valet, sometimes as
decimal strings (Pydantic serializes ``Decimal`` as a string), so a numeric
string is accepted; ``bool`` and ``None`` are not numbers here.
"""

from __future__ import annotations

import math
from typing import Any


def _as_float(value: Any, name: str) -> float:
    """``value`` as a float (possibly non-finite); refuses null, bool, non-numbers."""
    if value is None:
        raise ValueError(f"{name} is required (got null)")
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, got a boolean ({value!r})")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}") from None


def finite_number(value: Any, name: str) -> float:
    """``value`` as a finite float, or ``ValueError`` naming ``name``."""
    number = _as_float(value, name)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return number


def positive_number(value: Any, name: str) -> float:
    """A finite number strictly above zero."""
    number = _as_float(value, name)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be positive and finite, got {value!r}")
    return number


def non_negative_number(value: Any, name: str) -> float:
    """A finite number at or above zero."""
    number = _as_float(value, name)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be non-negative and finite, got {value!r}")
    return number


def non_negative_int(value: Any, name: str) -> int:
    """A whole, non-negative count (an integral float like ``3.0`` is accepted)."""
    number = non_negative_number(value, name)
    if not number.is_integer():
        raise ValueError(f"{name} must be a whole number, got {number:g}")
    return int(number)


def percentage(value: Any, name: str, *, allow_zero: bool = False) -> float:
    """A percentage in ``(0, 100]`` (or ``[0, 100]`` with ``allow_zero``)."""
    number = finite_number(value, name)
    lower_ok = number >= 0 if allow_zero else number > 0
    if not lower_ok or number > 100:
        interval = "[0, 100]" if allow_zero else "(0, 100]"
        raise ValueError(f"{name} must be a percentage in {interval}, got {number:g}")
    return number


def valid_nav(value: Any, name: str = "nav") -> float:
    """A usable NAV: finite and strictly positive. It is the denominator of every
    percentage limit, so a NaN, infinite, zero or negative NAV is refused."""
    return positive_number(value, name)
