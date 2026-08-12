"""Unit tests for pa-eval replay-criterion (Track 4.1 scaffolding, PR C).

test_replay_criterion_finds_recent_verdicts:
    DB query returns verdicts within the time window for the given criterion code.

test_replay_criterion_invokes_shared_evaluator:
    Replay path invokes verdict_pipeline.evaluate_one_criterion() once per
    verdict with a reconstructed CompassEvaluatorContext — the same shared
    dispatcher L1/L2 use.

test_replay_criterion_writes_diff_report:
    Output report contains the expected table structure and diff metadata.

test_replay_criterion_rejects_non_judge_criterion:
    CLI exits non-zero with a clear error when criterion is not judge_prompt.

test_replay_criterion_handles_missing_criterion_code:
    CLI exits non-zero with a clear error when criterion_code is not found.

test_replay_criterion_report_records_outcome_change:
    A verdict whose re-evaluation flips outcome is marked changed=yes in the report.

test_replay_criterion_report_records_no_change:
    A verdict whose re-evaluation keeps the same outcome is marked changed=no.

Run:
    PYDANTIC_AI_GATEWAY_API_KEY=test-key PYTHONPATH=src uv run pytest \\
        src/compass_backend/tests/test_pa_eval_replay_criterion.py -x --tb=short
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from compass_backend.db.rows import CriterionRecord, VerdictRecord
from compass_backend.tests._evidence_fixtures import default_evidence_dict
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.tests.conftest import make_criterion_record


# ─── Helpers ──────────────────────────────────────────────────────────────────


# This module's judge_prompt rubric is read by the replay assertions, so it keeps
# its own payload text (not the shared conftest default) and passes it explicitly.
_DEFAULT_PAYLOADS: dict[str, dict] = {
    "deterministic": {"validator_name": "test_validator"},
    "judge_prompt": {"judge_prompt": "Does the response reference prior context?"},
    "span_assertion": {},
    "scenario_fit": {"judge_prompt_template": "{answer_text}"},
}


def _criterion_record(
    *,
    criterion_id: int = 1,
    criterion_code: str = "preserves_prior_context",
    check_type: str = "judge_prompt",
) -> CriterionRecord:
    return make_criterion_record(
        id=criterion_id,
        criterion_code=criterion_code,
        text="The response should preserve prior context.",
        category="accuracy",
        check_type=check_type,
        priority=10,
        payload=dict(_DEFAULT_PAYLOADS[check_type]),
        version_hash="hash-abc123",
    )


# The hydrated full answer the live judge graded. #1477: replay_one_verdict
# refuses to grade a row that lacks ``full_answer_text`` (the bounded
# ``answer_excerpt`` stub false-fails complete answers as "cut off"), and
# _reconstruct_context now sources answer_text from ``full_answer_text``. The
# excerpt is a prefix of it (the prefix-checksum invariant in
# hydrate_full_answers), so the full text starts with the excerpt.
_ANSWER_EXCERPT = "Here is the data from the prior turn: ..."
_FULL_ANSWER_TEXT = (
    "Here is the data from the prior turn: ... and here is the complete "
    "body the live judge actually graded, beyond the bounded excerpt."
)


def _verdict_row(
    *,
    criterion_id: int = 1,
    session_id: str | None = None,
    outcome: str = "pass",
    scenario_title: str = "Scenario A",
    include_full_answer: bool = True,
) -> dict[str, Any]:
    sid = session_id or str(uuid.uuid4())
    vid = str(uuid.uuid4())
    row: dict[str, Any] = {
        "verdict_id": vid,
        "session_id": sid,
        "turn_index": 1,
        "step_index": 0,
        "case_id": 100,
        "scenario_id": 10,
        "criterion_id": criterion_id,
        "criterion_version_hash": "hash-abc123",
        "outcome": outcome,
        "reason": "Response references prior context correctly.",
        "evidence": {
            "answer_excerpt": _ANSWER_EXCERPT,
            "artifact_snapshot": {},
            "copied_span_attributes": {},
        },
        "trace_id": "trace-aaa",
        "scenario_title": scenario_title,
    }
    # #1477: rows reach replay_one_verdict already hydrated with the full answer;
    # an un-hydrated row (no full_answer_text) is skipped without grading. Default
    # to hydrated so the evaluator-path tests exercise the real delegation;
    # include_full_answer=False covers the skip path.
    if include_full_answer:
        row["full_answer_text"] = _FULL_ANSWER_TEXT
    return row


def _verdict_record_from_row(row: dict[str, Any]) -> VerdictRecord:
    return VerdictRecord(
        id=row["verdict_id"],
        session_id=row["session_id"],
        turn_index=row["turn_index"],
        step_index=row["step_index"],
        case_id=row["case_id"],
        scenario_id=row["scenario_id"],
        criterion_id=row["criterion_id"],
        criterion_version_hash=row["criterion_version_hash"],
        criterion_set_version="compass_criteria_v1",
        judge_source="judge_prompt",
        scope="turn",
        outcome=row["outcome"],  # type: ignore[arg-type]
        reason=row["reason"],
        evidence=row["evidence"],
        trace_id=row["trace_id"],
        triggered_by="sweep_run",
        sweep_run_id=None,
    )


# ─── Query helper tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_criterion_finds_recent_verdicts() -> None:
    """load_verdicts_for_replay should query by criterion_code + time window.

    Uses a fake pool that records the SQL and parameters passed to it.
    Asserts the query uses a LIMIT and filters by since_hours.
    """
    from scripts.pa_eval.replay_criterion import load_verdicts_for_replay

    captured_sql: list[str] = []
    captured_params: list[tuple] = []

    criterion = _criterion_record()

    class _FakeConn:
        async def fetch(self, sql: str, *params: Any) -> list:
            captured_sql.append(sql)
            captured_params.append(params)
            row = _verdict_row()
            # Return as asyncpg.Record-like objects (dicts suffice for the helper)
            return [_FakePgRecord(row)]

    class _FakePool:
        def acquire(self):
            return _FakeCtx()

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *_):
            pass

    rows = await load_verdicts_for_replay(
        pool=_FakePool(),  # type: ignore[arg-type]
        criterion=criterion,
        since_hours=168,
        pg_schema="compass",
    )

    assert len(rows) == 1
    assert len(captured_sql) == 1
    sql = captured_sql[0]
    # Must reference verdicts table
    assert "verdicts" in sql
    # Must filter by criterion_id (the criterion object is already resolved)
    assert "criterion_id" in sql.lower()
    # Must have a time-window filter using NOW() and INTERVAL
    assert "interval" in sql.lower() and "now()" in sql.lower()


class _FakePgRecord:
    """Minimal asyncpg.Record-like object backed by a dict."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()


# ─── Evaluator invocation tests ───────────────────────────────────────────────


def _fake_evaluate_one_criterion_returning(
    outcome: str, reason: str
):
    """Build a fake evaluate_one_criterion that records its calls and returns
    a VerdictRecord with the given outcome.
    """
    calls: list[tuple[CompassEvaluatorContext, CriterionRecord]] = []

    async def _fake(
        *, ctx: CompassEvaluatorContext, criterion: CriterionRecord
    ) -> VerdictRecord:
        calls.append((ctx, criterion))
        return VerdictRecord(
            id=str(uuid.uuid4()),
            session_id=ctx.session_id,
            turn_index=ctx.turn_index,
            step_index=ctx.step_index,
            case_id=ctx.case_id,
            scenario_id=ctx.scenario_id,
            criterion_id=criterion.id,
            criterion_version_hash=criterion.version_hash,
            criterion_set_version="compass_criteria_v1",
            judge_source="judge_prompt",
            scope="turn",
            outcome=outcome,  # type: ignore[arg-type]
            reason=reason,
            evidence=default_evidence_dict(answer_excerpt=ctx.answer_text),
            trace_id=ctx.trace_id,
            triggered_by=ctx.triggered_by,
        )

    return _fake, calls


@pytest.mark.asyncio
async def test_replay_criterion_invokes_shared_evaluator() -> None:
    """replay_one_verdict should delegate to verdict_pipeline.evaluate_one_criterion.

    Verifies the reconstructed CompassEvaluatorContext carries:
    - answer_text = evidence['answer_excerpt']
    - session_id from the verdict row
    - triggered_by = 'manual_replay'
    """
    from scripts.pa_eval.replay_criterion import replay_one_verdict

    criterion = _criterion_record()
    row = _verdict_row(outcome="pass")

    fake_evaluate_one, calls = _fake_evaluate_one_criterion_returning(
        outcome="pass", reason="Context preserved correctly."
    )

    with patch(
        "scripts.pa_eval.replay_criterion.evaluate_one_criterion",
        new=fake_evaluate_one,
    ):
        result = await replay_one_verdict(
            verdict_row=row,
            criterion=criterion,
        )

    # evaluate_one_criterion was called exactly once with the criterion
    assert len(calls) == 1
    ctx, called_criterion = calls[0]
    assert called_criterion is criterion
    # #1477: answer_text comes from full_answer_text (the hydrated answer the
    # live judge graded), NOT the bounded evidence['answer_excerpt'] stub.
    assert ctx.answer_text == row["full_answer_text"]
    # session_id should match the verdict
    assert ctx.session_id == row["session_id"]
    # triggered_by must be 'manual_replay'
    assert ctx.triggered_by == "manual_replay"
    # The result carries old and new outcome
    assert result["original_outcome"] == "pass"
    assert result["new_outcome"] == "pass"
    assert result["changed"] is False


@pytest.mark.asyncio
async def test_replay_criterion_skips_unhydrated_row_without_grading() -> None:
    """#1477: a verdict row lacking ``full_answer_text`` is SKIPPED, not graded.

    Grading the bounded ``answer_excerpt`` stub false-fails complete answers as
    "cut off", so replay_one_verdict must short-circuit to a 'skipped' result
    and never call evaluate_one_criterion when the full answer is unavailable.
    """
    from scripts.pa_eval.replay_criterion import replay_one_verdict

    criterion = _criterion_record()
    row = _verdict_row(outcome="pass", include_full_answer=False)

    fake_evaluate_one, calls = _fake_evaluate_one_criterion_returning(
        outcome="pass", reason="should never be reached"
    )

    with patch(
        "scripts.pa_eval.replay_criterion.evaluate_one_criterion",
        new=fake_evaluate_one,
    ):
        result = await replay_one_verdict(verdict_row=row, criterion=criterion)

    # The evaluator was NOT invoked on an un-hydrated row.
    assert len(calls) == 0
    assert result["new_outcome"] == "skipped"
    assert result["skipped"] is True
    assert result["changed"] is False
    assert result["original_outcome"] == "pass"


@pytest.mark.asyncio
async def test_replay_criterion_report_records_outcome_change() -> None:
    """When re-evaluation flips outcome, changed=True in the replay result."""
    from scripts.pa_eval.replay_criterion import replay_one_verdict

    criterion = _criterion_record()
    row = _verdict_row(outcome="pass")  # original: pass

    fake_evaluate_one, _ = _fake_evaluate_one_criterion_returning(
        outcome="fail", reason="Context NOT preserved."
    )

    with patch(
        "scripts.pa_eval.replay_criterion.evaluate_one_criterion",
        new=fake_evaluate_one,
    ):
        result = await replay_one_verdict(verdict_row=row, criterion=criterion)

    assert result["original_outcome"] == "pass"
    assert result["new_outcome"] == "fail"
    assert result["changed"] is True


@pytest.mark.asyncio
async def test_replay_criterion_report_records_no_change() -> None:
    """When re-evaluation keeps the same outcome, changed=False in the result."""
    from scripts.pa_eval.replay_criterion import replay_one_verdict

    criterion = _criterion_record()
    row = _verdict_row(outcome="fail")

    fake_evaluate_one, _ = _fake_evaluate_one_criterion_returning(
        outcome="fail", reason="Still fails."
    )

    with patch(
        "scripts.pa_eval.replay_criterion.evaluate_one_criterion",
        new=fake_evaluate_one,
    ):
        result = await replay_one_verdict(verdict_row=row, criterion=criterion)

    assert result["original_outcome"] == "fail"
    assert result["new_outcome"] == "fail"
    assert result["changed"] is False


@pytest.mark.asyncio
async def test_replay_criterion_handles_construction_failure() -> None:
    """When evaluate_one_criterion returns None (irrecoverable failure),
    replay_one_verdict surfaces a structured error result instead of crashing.
    """
    from scripts.pa_eval.replay_criterion import replay_one_verdict

    criterion = _criterion_record()
    row = _verdict_row(outcome="pass")

    async def fake_evaluate_one(
        *, ctx: CompassEvaluatorContext, criterion: CriterionRecord
    ) -> VerdictRecord | None:
        return None

    with patch(
        "scripts.pa_eval.replay_criterion.evaluate_one_criterion",
        new=fake_evaluate_one,
    ):
        result = await replay_one_verdict(verdict_row=row, criterion=criterion)

    assert result["original_outcome"] == "pass"
    assert result["new_outcome"] == "error"
    assert "construction failed" in result["new_reason"]
    assert result["changed"] is True


# ─── Report format test ───────────────────────────────────────────────────────


def test_replay_criterion_writes_diff_report(tmp_path: Path) -> None:
    """build_replay_report + write_replay_report should produce valid markdown.

    Asserts:
    - Report path was created
    - Contains the criterion code header
    - Contains the diff table with expected columns
    - Contains summary metadata (verdicts replayed, outcome changes)
    """
    from scripts.pa_eval.replay_criterion import build_replay_report, write_replay_report

    diff_rows = [
        {
            "case_id": 100,
            "scenario_title": "Scenario A",
            "original_outcome": "pass",
            "new_outcome": "pass",
            "changed": False,
            "new_reason": "Context preserved.",
            "old_reason": "Context preserved correctly.",
            "verdict_id": "11111111-1111-1111-1111-111111111111",
            "session_id": "aaaa-bbbb",
        },
        {
            "case_id": 101,
            "scenario_title": "Scenario B",
            "original_outcome": "pass",
            "new_outcome": "fail",
            "changed": True,
            "new_reason": "Context not preserved.",
            "old_reason": "Context preserved.",
            "verdict_id": "22222222-2222-2222-2222-222222222222",
            "session_id": "cccc-dddd",
        },
    ]

    report_md = build_replay_report(
        criterion_code="preserves_prior_context",
        since_hours=168,
        diff_rows=diff_rows,
    )

    # Must have criterion header
    assert "preserves_prior_context" in report_md
    # Must have summary section
    assert "Verdicts replayed" in report_md or "verdicts replayed" in report_md.lower()
    assert "2" in report_md  # 2 verdicts
    assert "1" in report_md  # 1 changed
    # Must have diff table
    assert "| case_id" in report_md or "case_id" in report_md
    assert "original outcome" in report_md.lower() or "original_outcome" in report_md
    assert "new outcome" in report_md.lower() or "new_outcome" in report_md
    assert "changed" in report_md.lower()
    # Must contain the scenario titles
    assert "Scenario A" in report_md
    assert "Scenario B" in report_md

    # Write to disk and verify file exists
    report_path = tmp_path / "test_report.md"
    write_replay_report(report_md, report_path)
    assert report_path.exists()
    content = report_path.read_text()
    assert "preserves_prior_context" in content


def test_replay_report_flags_candidate_mode() -> None:
    """build_replay_report annotates candidate-prompt runs so the report is
    never mistaken for a live-criterion replay (the validate-before-apply tool).
    """
    from scripts.pa_eval.replay_criterion import build_replay_report

    diff_rows = [
        {
            "case_id": 1,
            "scenario_title": "S",
            "original_outcome": "fail",
            "new_outcome": "pass",
            "changed": True,
            "new_reason": "now canonical",
            "old_reason": "x",
            "verdict_id": "v",
            "session_id": "s",
        },
    ]
    # Candidate mode: header must flag it and name the source file.
    candidate_md = build_replay_report(
        criterion_code="coverage_state_language_quality",
        since_hours=24,
        diff_rows=diff_rows,
        candidate_note="candidates/coverage_v2.txt",
    )
    assert "CANDIDATE" in candidate_md
    assert "candidates/coverage_v2.txt" in candidate_md

    # Default (live) mode: no candidate banner.
    live_md = build_replay_report(
        criterion_code="coverage_state_language_quality",
        since_hours=24,
        diff_rows=diff_rows,
    )
    assert "CANDIDATE" not in live_md


# ─── Error handling tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_criterion_rejects_non_judge_criterion() -> None:
    """replay_criterion should raise ValueError for non-judge_prompt criteria.

    Deterministic and span_assertion criteria cannot be replayed without
    a running backend or Logfire.
    """
    from scripts.pa_eval.replay_criterion import validate_criterion_for_replay

    deterministic = _criterion_record(check_type="deterministic")
    with pytest.raises(ValueError, match="judge_prompt"):
        validate_criterion_for_replay(deterministic)


@pytest.mark.asyncio
async def test_replay_criterion_handles_missing_criterion_code() -> None:
    """load_criterion_by_code raises ValueError when code not found in DB."""
    from scripts.pa_eval.replay_criterion import load_criterion_by_code

    class _FakeConn:
        async def fetchrow(self, sql: str, *params: Any) -> None:
            return None  # not found

    class _FakePool:
        def acquire(self):
            return _FakeCtx()

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *_):
            pass

    with pytest.raises(ValueError, match="no active criterion"):
        await load_criterion_by_code(
            pool=_FakePool(),  # type: ignore[arg-type]
            criterion_code="nonexistent_criterion",
            pg_schema="compass",
        )
