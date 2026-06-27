"""Guard the SHIPPED config/risk_profile.yaml against the spec's approved
defaults (spec §"Risco e persistência" + §F). If someone retunes the aggressive
profile away from the spec, this fails loudly."""

from __future__ import annotations

from pathlib import Path

from vizier import risk

_CONFIG = Path(__file__).resolve().parent.parent / "config" / "risk_profile.yaml"


def test_shipped_default_is_aggressive():
    profile = risk.load_profile(_CONFIG)  # resolves active_profile
    assert profile.name == "aggressive"


def test_aggressive_matches_spec():
    p = risk.load_profile(_CONFIG, "aggressive")
    assert p.max_pct_per_asset == 25
    assert p.max_pct_per_sector == 40
    assert p.min_cash_pct == 5
    assert p.max_positions == 8
    assert p.circuit_breaker.vix_threshold == 38
    assert p.circuit_breaker.monthly_drawdown_pct == 18
    assert p.autonomy.per_run_pct == 33
    assert p.autonomy.per_run_max_trades == 5
    assert p.autonomy.daily_cumulative_pct == 50
    assert p.conviction_floor == 2
    assert p.conviction_full_size_pct_of_cap == 65  # conviction-5 -> 65% of cap


def test_all_three_profiles_load():
    for name in ("conservative", "moderate", "aggressive"):
        assert risk.load_profile(_CONFIG, name).name == name


def test_tighter_profiles_are_actually_tighter():
    conservative = risk.load_profile(_CONFIG, "conservative")
    aggressive = risk.load_profile(_CONFIG, "aggressive")
    assert conservative.max_pct_per_asset < aggressive.max_pct_per_asset
    assert conservative.min_cash_pct > aggressive.min_cash_pct
    assert conservative.autonomy.daily_cumulative_pct < aggressive.autonomy.daily_cumulative_pct
