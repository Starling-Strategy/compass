"""Tests for resilient case-row loading in db/scenarios_v2.py.

Locks in the defensive contract added after PR #625's Cluster 6 finding:
a single malformed compass.cases row (e.g. CONS-MIGRATED-302-C00's
[{"prompt": null}] inputs) must NOT abort an entire dimension sweep. The
loader skips bad rows with a structured warning instead of raising.

Run with:
    PYTHONPATH=src .venv/bin/python -m pytest \
        src/compass_backend/tests/test_scenarios_v2_case_loading.py --tb=short
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from compass_backend.db import scenarios_v2
from compass_backend.db.rows import CaseRecord
from compass_backend.db.scenarios_v2 import (
    ReadOnlyScenariosV2Repository,
    _case_from_row,
)


def _well_formed_row(
    *,
    case_id: int = 1,
    case_code: str = "TEST-CASE-C00",
    scenario_id: int = 10,
    prompt: str = "What is the starting salary in Denver?",
) -> dict:
    """A row-shaped dict that maps onto `compass.cases` column names."""

    return {
        "id": case_id,
        "scenario_id": scenario_id,
        "case_code": case_code,
        "inputs": [{"prompt": prompt}],
        "expected_output": {
            "expected_behaviour": "Compass returns the salary value.",
            "steps": [{"step_index": 0, "expected_output": "$50,000"}],
        },
        "metadata": {"phrasing": "primary"},
        "version_hash": "hash-abc-123",
        "active": True,
    }


def _malformed_row(
    *,
    case_id: int = 999,
    case_code: str = "BAD-CASE-C00",
    scenario_id: int = 99,
) -> dict:
    """Row with null prompt — mirrors CONS-MIGRATED-302-C00's broken shape."""

    return {
        "id": case_id,
        "scenario_id": scenario_id,
        "case_code": case_code,
        "inputs": [{"prompt": None}],
        "expected_output": {
            "expected_behaviour": "Should never be reached.",
            "steps": [{"step_index": 0, "expected_output": ""}],
        },
        "metadata": {},
        "version_hash": "hash-bad-000",
        "active": True,
    }


# ─── 1. Well-formed row produces a CaseRecord ────────────────────────────────


def test_case_from_row_well_formed_returns_case_record() -> None:
    """The happy path: a well-formed row builds a CaseRecord with correct fields."""
    row = _well_formed_row()
    case = _case_from_row(row)
    assert case is not None
    assert isinstance(case, CaseRecord)
    assert case.case_code == "TEST-CASE-C00"
    assert case.scenario_id == 10
    assert len(case.inputs) == 1
    assert case.inputs[0].prompt == "What is the starting salary in Denver?"
    assert case.expected_output.expected_behaviour == "Compass returns the salary value."


def test_case_from_row_normalizes_legacy_step_expectation_shape() -> None:
    row = _well_formed_row()
    row["expected_output"] = {
        "expected_behaviour": "Compass returns a ranked FRPL table.",
        "steps": [
            {
                "prompt": "what are the districts with the highest FRPL share?",
                "expected_behaviour": "Ranks districts by FRPL without refusal.",
            }
        ],
    }

    case = _case_from_row(row)

    assert case is not None
    assert case.expected_output.steps[0].step_index == 0
    assert (
        case.expected_output.steps[0].expected_output
        == "Ranks districts by FRPL without refusal."
    )


def test_case_from_row_normalizes_expected_output_only_step_shape() -> None:
    row = _well_formed_row()
    row["expected_output"] = {
        "expected_behaviour": "Compass routes a peer comparison request.",
        "steps": [{"expected_output": "Routes to peer-comparison execution."}],
    }

    case = _case_from_row(row)

    assert case is not None
    assert case.expected_output.steps[0].step_index == 0
    assert case.expected_output.steps[0].expected_output == (
        "Routes to peer-comparison execution."
    )


# ─── 2. Null-prompt row is skipped (not raised) ──────────────────────────────


def test_case_from_row_with_null_prompt_returns_none(monkeypatch) -> None:
    """A null-prompt row returns None and logs a structured skip warning.

    Regression for PR #625 Cluster 6: before this fix, a list comprehension
    would let StepInput.model_validate raise ValidationError, killing the
    entire pa-eval dimension sweep on the first bad row.
    """
    warn_calls: list[tuple[str, dict]] = []

    def _spy(event: str, **kwargs) -> None:
        warn_calls.append((event, kwargs))

    monkeypatch.setattr(scenarios_v2, "log_warn", _spy)

    row = _malformed_row()
    case = _case_from_row(row)
    assert case is None
    assert len(warn_calls) == 1
    event, kwargs = warn_calls[0]
    assert event == "compass.cases.row_skipped_invalid_shape"
    assert kwargs["case_code"] == "BAD-CASE-C00"
    assert kwargs["case_id"] == 999
    assert kwargs["scenario_id"] == 99


def test_case_from_row_with_partial_null_inputs_returns_none(monkeypatch) -> None:
    """If any step in inputs has a null prompt, the whole case is skipped."""
    warn_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        scenarios_v2,
        "log_warn",
        lambda event, **kwargs: warn_calls.append((event, kwargs)),
    )

    row = _well_formed_row()
    row["inputs"] = [{"prompt": None}, {"prompt": "valid second step"}]
    case = _case_from_row(row)
    assert case is None
    assert len(warn_calls) == 1


# ─── 3. load_cases_for_scenario filters out None returns ─────────────────────


@pytest.mark.asyncio
async def test_load_cases_for_scenario_filters_invalid_rows(monkeypatch) -> None:
    """Two rows in DB (one good, one malformed) → repository returns 1 CaseRecord.

    This is the regression-shape test that proves the original
    Consistency-dimension fatal-error class of bug can't recur: a malformed
    case row in scenario X does not prevent loading the other cases.
    """
    warn_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        scenarios_v2,
        "log_warn",
        lambda event, **kwargs: warn_calls.append((event, kwargs)),
    )

    good = _well_formed_row(case_id=1, case_code="GOOD-C00", scenario_id=42)
    bad = _malformed_row(case_id=2, case_code="BAD-C00", scenario_id=42)

    mock_conn = MagicMock()
    mock_conn.fetch = AsyncMock(return_value=[good, bad])

    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    mock_pool = MagicMock()
    mock_pool.acquire = _acquire

    repo = ReadOnlyScenariosV2Repository(pool=mock_pool)
    cases = await repo.load_cases_for_scenario(scenario_id=42)

    assert len(cases) == 1
    assert cases[0].case_code == "GOOD-C00"
    # The bad row triggered exactly one skip-warning with its case_code.
    assert len(warn_calls) == 1
    assert warn_calls[0][1]["case_code"] == "BAD-C00"
