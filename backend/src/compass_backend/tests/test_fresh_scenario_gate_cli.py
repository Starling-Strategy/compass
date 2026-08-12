"""Regression coverage for the active fresh Compass scenario-gate CLI."""

from __future__ import annotations

import pathlib


def test_active_fresh_scenario_gate_cli_is_not_archived() -> None:
    script = pathlib.Path("scripts/run_fresh_scenario_gate.py")

    assert script.exists()
    text = script.read_text()
    module_text = pathlib.Path(
        "src/compass_backend/testing/fresh_scenario_gate.py"
    ).read_text()
    assert "src.archive" not in text
    assert "compass_backend.testing.fresh_scenario_gate" in text
    for option in (
        "--feature",
        "--scenario-ids",
        "--title-contains",
        "--limit",
        "--offset",
        "--concurrency",
        "--output",
    ):
        assert option in module_text


def test_m1_closure_gate_pins_case_set_and_finalize_mode() -> None:
    script = pathlib.Path("scripts/run_m1_closure_gate.py")
    module_text = pathlib.Path(
        "src/compass_backend/testing/m1_closure_gate.py"
    ).read_text()

    assert script.exists()
    assert "compass_backend.testing.m1_closure_gate" in script.read_text()
    for case_id in ("365", "392", "436", "955", "1006", "1009", "1012", "1024", "323"):
        assert case_id in module_text
    assert "COMPASS_PLANNING_RECOGNITION_MODE=finalize" in module_text


def test_m1_closure_gate_requires_finalize_mode(monkeypatch) -> None:
    from compass_backend.testing.m1_closure_gate import (
        M1ClosureGateModeError,
        require_planning_recognition_finalize,
    )

    monkeypatch.delenv("COMPASS_PLANNING_RECOGNITION_MODE", raising=False)

    require_planning_recognition_finalize({})
    require_planning_recognition_finalize(
        {"COMPASS_PLANNING_RECOGNITION_MODE": "finalize"}
    )
    try:
        require_planning_recognition_finalize(
            {"COMPASS_PLANNING_RECOGNITION_MODE": "observe"}
        )
    except M1ClosureGateModeError as exc:
        assert "finalize" in str(exc)
    else:  # pragma: no cover - defensive assertion clarity
        raise AssertionError("M1 closure gate must require finalize mode")
