"""Behavioral tests for the CLI envelope: JSON in, ``{ok, data}`` out."""

from __future__ import annotations

import json

from vizier import cli


def _run(capsys, argv) -> dict:
    code = cli.main(argv)
    out = capsys.readouterr().out.strip()
    envelope = json.loads(out)
    envelope["_exit_code"] = code
    return envelope


def test_cli_ceiling_blocks_drain(capsys, profile_path):
    env = _run(
        capsys,
        [
            "ceiling",
            "--profile-path",
            str(profile_path),
            "--json",
            json.dumps(
                {
                    "baseline_nav_start_of_day": 1_000,
                    "spent_today": 480,
                    "trades_today": 3,
                    "candidate_value": 50,
                }
            ),
        ],
    )
    assert env["ok"] is True
    assert env["_exit_code"] == 0
    assert env["data"]["allowed"] is False


def test_cli_size_envelope(capsys, profile_path):
    env = _run(
        capsys,
        [
            "size",
            "--profile-path",
            str(profile_path),
            "--json",
            json.dumps({"slot_base": 100, "conviction": 5, "nav": 10_000}),
        ],
    )
    assert env["ok"] is True
    assert env["data"]["size"] == 65.0  # 100 * 5/5 * 0.65 (conviction-full-size knob)


def test_cli_allocate_mixed_explicit_and_equal(capsys, profile_path):
    # Per-candidate explicit keeps the user-named sub-floor leg; equal weighting
    # threads through the CLI and splits the kept legs evenly.
    env = _run(
        capsys,
        [
            "allocate",
            "--profile-path",
            str(profile_path),
            "--json",
            json.dumps(
                {
                    "total_amount": 100,
                    "nav": 100_000,
                    "weighting": "equal",
                    "candidates": [
                        {"ticker": "GOOD", "conviction": 4},
                        {"ticker": "MINE", "conviction": 1, "explicit": True},
                        {"ticker": "WEAK", "conviction": 1},
                    ],
                }
            ),
        ],
    )
    assert env["ok"] is True
    assert env["data"]["skipped"] == ["WEAK"]
    sizes = {a["ticker"]: a["size"] for a in env["data"]["allocations"]}
    assert sizes == {"GOOD": 50.0, "MINE": 50.0}
    assert env["data"]["weighting"] == "equal"


def test_cli_allocate_missing_total_amount_is_clear(capsys, profile_path):
    # A missing required field must read clearly, not leak a bare KeyError
    # message ({"error": "'total_amount'"}).
    env = _run(
        capsys,
        [
            "allocate",
            "--profile-path",
            str(profile_path),
            "--json",
            json.dumps(
                {
                    "nav": 100_000,
                    "candidates": [{"ticker": "AAA", "conviction": 4}],
                }
            ),
        ],
    )
    assert env["ok"] is False
    assert env["_exit_code"] == 1
    assert env["error"] == "missing required field: total_amount"


def test_cli_data_sufficiency_abstain(capsys):
    env = _run(
        capsys,
        [
            "data-sufficiency",
            "--json",
            json.dumps(
                {
                    "scout_responses": {"price": None, "pe": 20, "sector": "Tech"},
                    "decision_type": "valuation",
                }
            ),
        ],
    )
    assert env["ok"] is True
    assert env["data"]["recommended_action"] == "abstain"


def test_cli_thesis_roundtrip_via_memory_dir(capsys, memory_dir):
    thesis = {
        "ticker": "ACME",
        "horizon_tag": "core",
        "open_date": "2026-01-15",
        "entry_price": 142.3,
        "qty": 7,
        "conviction": 4,
        "thesis_one_liner": "x",
        "reason": "y",
        "main_risk": "z",
        "review_trigger": {"type": "price", "value": 120.0},
        "review_trigger_is_hard_stop": True,
        "baseline_snapshot": {"price": 142.3, "pe": 21.4},
    }
    write = _run(
        capsys,
        ["write-thesis", "--memory-dir", str(memory_dir), "--json", json.dumps(thesis)],
    )
    assert write["ok"] is True

    listed = _run(capsys, ["list-theses", "--memory-dir", str(memory_dir), "--json", "{}"])
    assert [t["ticker"] for t in listed["data"]] == ["ACME"]


def test_cli_trim_qty_with_tranche_check(capsys, memory_dir):
    # Seed a 5-share tactical tranche so the cross-check has balances to read.
    thesis = {
        "ticker": "ETH/USDT", "horizon_tag": "tactical", "open_date": "2026-01-15",
        "entry_price": 2_500, "qty": 5, "conviction": 4, "thesis_one_liner": "x",
        "reason": "y", "main_risk": "z", "review_trigger": {"type": "price", "value": 2_000},
        "review_trigger_is_hard_stop": False, "baseline_snapshot": {"price": 2_500},
    }
    _run(capsys, ["write-thesis", "--memory-dir", str(memory_dir), "--json", json.dumps(thesis)])

    env = _run(
        capsys,
        [
            "trim-qty", "--memory-dir", str(memory_dir), "--json",
            json.dumps({"ticker": "ETH/USDT", "tag": "tactical", "current_qty": 5, "pct": 30}),
        ],
    )
    assert env["ok"] is True
    assert env["data"]["qty"] == 1.5  # 30% of 5
    assert env["data"]["tranche_check"]["allowed"] is True  # 1.5 <= 5 tactical


def test_cli_build_own_sent_orders(capsys, memory_dir):
    _run(
        capsys,
        [
            "append-decision", "--memory-dir", str(memory_dir), "--json",
            json.dumps({
                "timestamp": "2026-06-27T12:00:00+00:00", "intent": "buy AAA",
                "executed_orders": [{"side": "BUY", "ticker": "AAA", "value": 200.0}],
            }),
        ],
    )
    env = _run(
        capsys,
        ["build-own-sent-orders", "--memory-dir", str(memory_dir), "--json",
         json.dumps({"ticker": "AAA"})],
    )
    assert env["ok"] is True
    assert env["data"]["count"] == 1
    assert env["data"]["own_sent_orders"][0]["status"] == "filled"


def test_cli_rearm_mid_window_is_refused(capsys, memory_dir):
    """The drain guard surfaces as ok:false through the CLI (exit 1)."""
    armed = _run(
        capsys,
        ["arm-autonomy", "--memory-dir", str(memory_dir), "--json",
         json.dumps({"nav": 1_000, "now": "2026-06-27T12:00:00+00:00"})],
    )
    assert armed["ok"] is True
    refused = _run(
        capsys,
        ["arm-autonomy", "--memory-dir", str(memory_dir), "--json",
         json.dumps({"nav": 800, "now": "2026-06-27T14:00:00+00:00"})],
    )
    assert refused["ok"] is False
    assert refused["_exit_code"] == 1
    assert refused["error_type"] == "AutonomyAlreadyArmedError"


def test_cli_reports_error_envelope(capsys, profile_path):
    # Missing required payload key -> ok:false with a message, exit code 1.
    env = _run(
        capsys,
        ["size", "--profile-path", str(profile_path), "--json", json.dumps({"conviction": 5})],
    )
    assert env["ok"] is False
    assert env["_exit_code"] == 1
    assert "error" in env


def test_cli_bad_json_payload_is_reported(capsys):
    env = _run(capsys, ["breaker", "--json", "not-json"])
    assert env["ok"] is False
    assert env["_exit_code"] == 1


def test_cli_envelope_is_pure_ascii(capsys):
    """The envelope must be ASCII-only so it never crashes on a non-UTF-8 console
    (Windows cp1252). reconcile's exposure_source used to carry a Unicode union
    symbol that broke `print` on Windows — guard against any regression."""
    cli.main(
        [
            "reconcile",
            "--json",
            json.dumps(
                {
                    "ticker": "AAA",
                    "broker_positions": [],
                    "own_sent_orders": [{"ticker": "AAA", "side": "buy", "status": "submitted"}],
                }
            ),
        ]
    )
    out = capsys.readouterr().out
    out.encode("ascii")  # raises UnicodeEncodeError if any non-ASCII leaked
    assert json.loads(out)["ok"] is True
