"""Regression coverage for the M2 + M3 + M5 closure gates (W0-05, Refs #819)."""

from __future__ import annotations

import asyncio
import inspect
import pathlib

import pytest


def _read(path: str) -> str:
    return pathlib.Path(path).read_text()


# --------------------------------------------------------------------------- #
# M2 closure gate                                                              #
# --------------------------------------------------------------------------- #


def test_m2_closure_gate_pins_case_set_and_finalize_mode() -> None:
    module_text = _read("src/compass_backend/testing/m2_closure_gate.py")

    # Pinned case codes trace explicitly to the seed migrations.
    for case_code in (
        "REGR-M2-PEER-DENVER-SICK-LEAVE-C00",
        "REGR-M2-PEER-SAN-BERNARDINO-SALARY-C00",
    ):
        assert case_code in module_text, case_code
    assert "COMPASS_PLANNING_RECOGNITION_MODE=finalize" in module_text


def test_m2_pinned_case_list_is_non_empty_and_unique() -> None:
    from compass_backend.testing.m2_closure_gate import M2_CLOSURE_CASE_CODES

    assert isinstance(M2_CLOSURE_CASE_CODES, tuple)
    assert len(M2_CLOSURE_CASE_CODES) > 0
    assert all(isinstance(code, str) and code for code in M2_CLOSURE_CASE_CODES)
    assert len(set(M2_CLOSURE_CASE_CODES)) == len(M2_CLOSURE_CASE_CODES)


def test_m2_closure_gate_requires_finalize_mode() -> None:
    from compass_backend.testing.m2_closure_gate import (
        M2ClosureGateModeError,
        require_planning_recognition_finalize,
    )

    require_planning_recognition_finalize(
        {"COMPASS_PLANNING_RECOGNITION_MODE": "finalize"}
    )
    with pytest.raises(M2ClosureGateModeError) as excinfo:
        require_planning_recognition_finalize(
            {"COMPASS_PLANNING_RECOGNITION_MODE": "observe"}
        )
    assert "finalize" in str(excinfo.value)


def test_m2_closure_gate_invokes_finalize_helper(monkeypatch) -> None:
    """``run_m2_closure_gate`` must call the finalize-mode helper exactly the
    way m1 does (same module, same function name, called unconditionally when
    ``require_finalize`` is true)."""

    from compass_backend.testing import m2_closure_gate as gate

    finalize_calls: list[None] = []

    def _record_finalize() -> None:
        finalize_calls.append(None)

    fake_results: list[object] = []

    async def _fake_runner(**kwargs) -> list[object]:
        # Should be called with the pinned code list.
        assert kwargs["case_codes"] == list(gate.M2_CLOSURE_CASE_CODES)
        return fake_results

    monkeypatch.setattr(
        gate, "require_planning_recognition_finalize", _record_finalize
    )
    monkeypatch.setattr(gate, "run_fresh_scenario_cases_by_code", _fake_runner)

    result = asyncio.run(
        gate.run_m2_closure_gate(api_url="http://localhost", require_finalize=True)
    )

    assert finalize_calls == [None]
    assert result is fake_results


def test_m2_finalize_helper_matches_m1_signature() -> None:
    from compass_backend.testing.m1_closure_gate import (
        require_planning_recognition_finalize as m1_helper,
    )
    from compass_backend.testing.m2_closure_gate import (
        require_planning_recognition_finalize as m2_helper,
    )

    assert inspect.signature(m1_helper) == inspect.signature(m2_helper)


# --------------------------------------------------------------------------- #
# M3 closure gate                                                              #
# --------------------------------------------------------------------------- #


def test_m3_closure_gate_pins_case_set_and_finalize_mode() -> None:
    module_text = _read("src/compass_backend/testing/m3_closure_gate.py")

    for case_code in (
        "MULTI-TURN-2026-05-FOLLOWUP-001-C00",
        "MULTI-TURN-2026-05-FOLLOWUP-002-C00",
        "MULTI-TURN-2026-05-FOLLOWUP-003-C00",
        "MULTI-TURN-2026-05-FOLLOWUP-004-C00",
        "MULTI-TURN-2026-05-FOLLOWUP-005-C00",
        "MULTI-TURN-2026-05-FOLLOWUP-006-C00",
        "MULTI-TURN-2026-05-FOLLOWUP-007-C00",
        "MULTI-TURN-2026-05-FOLLOWUP-008-C00",
    ):
        assert case_code in module_text, case_code
    assert "COMPASS_PLANNING_RECOGNITION_MODE=finalize" in module_text


def test_m3_pinned_case_list_is_non_empty_and_unique() -> None:
    from compass_backend.testing.m3_closure_gate import M3_CLOSURE_CASE_CODES

    assert isinstance(M3_CLOSURE_CASE_CODES, tuple)
    assert len(M3_CLOSURE_CASE_CODES) > 0
    assert all(isinstance(code, str) and code for code in M3_CLOSURE_CASE_CODES)
    assert len(set(M3_CLOSURE_CASE_CODES)) == len(M3_CLOSURE_CASE_CODES)


def test_m3_closure_gate_requires_finalize_mode() -> None:
    from compass_backend.testing.m3_closure_gate import (
        M3ClosureGateModeError,
        require_planning_recognition_finalize,
    )

    require_planning_recognition_finalize(
        {"COMPASS_PLANNING_RECOGNITION_MODE": "finalize"}
    )
    with pytest.raises(M3ClosureGateModeError) as excinfo:
        require_planning_recognition_finalize(
            {"COMPASS_PLANNING_RECOGNITION_MODE": "observe"}
        )
    assert "finalize" in str(excinfo.value)


def test_m3_closure_gate_invokes_finalize_helper(monkeypatch) -> None:
    from compass_backend.testing import m3_closure_gate as gate

    finalize_calls: list[None] = []

    def _record_finalize() -> None:
        finalize_calls.append(None)

    fake_results: list[object] = []

    async def _fake_runner(**kwargs) -> list[object]:
        assert kwargs["case_codes"] == list(gate.M3_CLOSURE_CASE_CODES)
        return fake_results

    monkeypatch.setattr(
        gate, "require_planning_recognition_finalize", _record_finalize
    )
    monkeypatch.setattr(gate, "run_fresh_scenario_cases_by_code", _fake_runner)

    result = asyncio.run(
        gate.run_m3_closure_gate(api_url="http://localhost", require_finalize=True)
    )

    assert finalize_calls == [None]
    assert result is fake_results


def test_m3_finalize_helper_matches_m1_signature() -> None:
    from compass_backend.testing.m1_closure_gate import (
        require_planning_recognition_finalize as m1_helper,
    )
    from compass_backend.testing.m3_closure_gate import (
        require_planning_recognition_finalize as m3_helper,
    )

    assert inspect.signature(m1_helper) == inspect.signature(m3_helper)


# --------------------------------------------------------------------------- #
# M5 closure gate                                                              #
# --------------------------------------------------------------------------- #


def test_m5_closure_gate_pins_case_set_and_finalize_mode() -> None:
    module_text = _read("src/compass_backend/testing/m5_closure_gate.py")

    # Pinned case codes trace explicitly to the seed migrations: 065/069/067/062/071.
    for case_code in (
        "REGR-W2-M5-01-COMPOSITE-CSV-NOT-BLANK-C00",
        "REGR-W2-M5-02-CITATION-TITLE-RESOLVED-C00",
        "REGR-W2-M5-03-FRPL-LOOKUP-CITATION-C00",
        "REGR-W0-01-PROFILE-RANK-PROOF-C00",
        "REGR-W2-M5-04-SOURCES-DEDUP-C00",
    ):
        assert case_code in module_text, case_code
    assert "COMPASS_PLANNING_RECOGNITION_MODE=finalize" in module_text


def test_m5_pinned_case_list_is_non_empty_and_unique() -> None:
    from compass_backend.testing.m5_closure_gate import M5_CLOSURE_CASE_CODES

    assert isinstance(M5_CLOSURE_CASE_CODES, tuple)
    assert len(M5_CLOSURE_CASE_CODES) > 0
    assert all(isinstance(code, str) and code for code in M5_CLOSURE_CASE_CODES)
    assert len(set(M5_CLOSURE_CASE_CODES)) == len(M5_CLOSURE_CASE_CODES)


def test_m5_closure_gate_requires_finalize_mode() -> None:
    from compass_backend.testing.m5_closure_gate import (
        M5ClosureGateModeError,
        require_planning_recognition_finalize,
    )

    require_planning_recognition_finalize(
        {"COMPASS_PLANNING_RECOGNITION_MODE": "finalize"}
    )
    with pytest.raises(M5ClosureGateModeError) as excinfo:
        require_planning_recognition_finalize(
            {"COMPASS_PLANNING_RECOGNITION_MODE": "observe"}
        )
    assert "finalize" in str(excinfo.value)


def test_m5_closure_gate_invokes_finalize_helper(monkeypatch) -> None:
    from compass_backend.testing import m5_closure_gate as gate

    finalize_calls: list[None] = []

    def _record_finalize() -> None:
        finalize_calls.append(None)

    fake_results: list[object] = []

    async def _fake_runner(**kwargs) -> list[object]:
        assert kwargs["case_codes"] == list(gate.M5_CLOSURE_CASE_CODES)
        return fake_results

    monkeypatch.setattr(
        gate, "require_planning_recognition_finalize", _record_finalize
    )
    monkeypatch.setattr(gate, "run_fresh_scenario_cases_by_code", _fake_runner)

    result = asyncio.run(
        gate.run_m5_closure_gate(api_url="http://localhost", require_finalize=True)
    )

    assert finalize_calls == [None]
    assert result is fake_results


def test_m5_finalize_helper_matches_m1_signature() -> None:
    from compass_backend.testing.m1_closure_gate import (
        require_planning_recognition_finalize as m1_helper,
    )
    from compass_backend.testing.m5_closure_gate import (
        require_planning_recognition_finalize as m5_helper,
    )

    assert inspect.signature(m1_helper) == inspect.signature(m5_helper)


def test_m5_closure_gate_covers_one_case_per_wave2_m5_issue() -> None:
    """Wave 2 exit criterion #4 requires one pinned case per closed M5
    issue (#745, #746, #747, #748). The gate also pins the W0-01 profile-
    rank case alongside REGR-W2-M5-03 because the W2-M5-03 fix touches
    both profile paths (rank + lookup) and the issue body cites both as
    blocking regressions — pinning only the lookup variant would leave
    the rank path unsentinelled."""

    from compass_backend.testing.m5_closure_gate import M5_CLOSURE_CASE_CODES

    code_text = " ".join(M5_CLOSURE_CASE_CODES)
    for required_token in (
        "REGR-W2-M5-01",  # #745
        "REGR-W2-M5-02",  # #746
        "REGR-W2-M5-03",  # #747 - profile_lookup path
        "REGR-W0-01",     # #747 - profile_rank path
        "REGR-W2-M5-04",  # #748
    ):
        assert required_token in code_text, required_token


# --------------------------------------------------------------------------- #
# fresh_scenario_gate smoke-only labelling (audit P2.f)                        #
# --------------------------------------------------------------------------- #


def test_fresh_scenario_gate_is_labelled_smoke_only() -> None:
    module_text = _read("src/compass_backend/testing/fresh_scenario_gate.py")
    # Module banner must explicitly call out smoke-only intent.
    assert "SMOKE-ONLY" in module_text
