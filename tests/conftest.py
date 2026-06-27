"""Shared fixtures. Tests build their own known profile YAML so they stay green
regardless of any future tweak to the shipped ``config/risk_profile.yaml``."""

from __future__ import annotations

from pathlib import Path

import pytest

from vizier import risk

# A fixed test profile mirroring the aggressive numbers, decoupled from the
# repo config so risk-math assertions never break when Pedro retunes config.
_TEST_PROFILE_YAML = """
active_profile: aggressive
conviction_floor: 2
profiles:
  aggressive:
    max_pct_per_asset: 25
    max_pct_per_sector: 40
    min_cash_pct: 5
    max_positions: 8
    conviction_full_size_pct_of_cap: 65
    circuit_breaker:
      vix_threshold: 38
      monthly_drawdown_pct: 18
    autonomy:
      per_run_pct: 33
      per_run_max_trades: 5
      daily_cumulative_pct: 50
      daily_max_trades: 12
      drawdown_kill_pct: 15
"""


@pytest.fixture
def profile_path(tmp_path: Path) -> Path:
    path = tmp_path / "risk_profile.yaml"
    path.write_text(_TEST_PROFILE_YAML, encoding="utf-8")
    return path


@pytest.fixture
def profile(profile_path: Path) -> risk.RiskProfile:
    return risk.load_profile(profile_path)


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    path = tmp_path / "memory"
    (path / "theses").mkdir(parents=True)
    return path
