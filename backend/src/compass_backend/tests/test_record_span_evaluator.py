"""Tests for RecordSpanEvaluator (Spec B post-spine Gap 4 PR C).

Covers both evaluation modes:
  1. target_spans populated — substring-match on span names (new Gap 4 PR C path).
  2. payload.assertion.tool populated, target_spans None — legacy 'naive
     tool called at all' path, kept for backwards compat.

Also covers:
  - Short-circuit error paths: no trace_id, no LOGFIRE_READ_TOKEN, empty trace.
  - CriterionRecord round-trip: db.criteria._criterion_from_row hydrates
    target_spans correctly from an asyncpg-shaped record.

All tests are offline. Logfire HTTP calls are short-circuited by mocking
either settings.logfire_read_token or _fetch_span_tree directly.

Run with:
    PYTHONPATH=src .venv/bin/python -m pytest \\
        src/compass_backend/tests/test_record_span_evaluator.py -x --tb=short
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr

from compass_backend.db.criteria import _criterion_from_row
from compass_backend.db.rows import CriterionRecord
from compass_backend.quality._evaluator_context import CompassEvaluatorContext
from compass_backend.quality import criteria as criteria_module
from compass_backend.quality.criteria import RecordSpanEvaluator
from compass_backend.tests.conftest import (
    make_criterion_record,
    make_evaluator_context,
)


# ─── Shared fixtures ──────────────────────────────────────────────────────────


def _ctx(
    *,
    trace_id: str | None = "trace-abc-123",
    triggered_by: str = "user_turn",
) -> CompassEvaluatorContext:
    return make_evaluator_context(
        session_id="11111111-1111-1111-1111-111111111111",
        turn_index=0,
        trace_id=trace_id,
        triggered_by=triggered_by,
    )


def _criterion(
    *,
    criterion_code: str = "auth_succeeded_for_user_turn",
    payload: dict | None = None,
    target_spans: list[str] | None = None,
) -> CriterionRecord:
    return make_criterion_record(
        id=42,
        criterion_code=criterion_code,
        category="process_integrity",
        check_type="span_assertion",
        priority=30,
        is_mandatory_global=True,
        payload=payload or {},
        target_spans=target_spans,
    )


# ─── 1. target_spans mode — happy path ───────────────────────────────────────


@pytest.mark.asyncio
async def test_passes_when_target_span_name_appears() -> None:
    """outcome='pass' when any target_spans entry substring-matches a span name."""
    c = _criterion(target_spans=["compass.auth.api_key"])
    ev = RecordSpanEvaluator(criterion=c)
    fake_trace = [
        {"span_name": "compass.turn", "attributes": {}},
        {"span_name": "compass.auth.api_key", "attributes": {"outcome": "success"}},
        {"span_name": "compass.session.load", "attributes": {}},
    ]
    with (
        patch(
            "compass_backend.quality.criteria.settings.logfire_read_token",
            SecretStr("fake-read-token"),
        ),
        patch(
            "compass_backend.quality.criteria._fetch_span_tree",
            AsyncMock(return_value=fake_trace),
        ),
    ):
        verdict = await ev.evaluate(_ctx())
    assert verdict.outcome == "pass"
    assert verdict.criterion_id == 42
    assert "matched target 'compass.auth.api_key'" in verdict.reason
    # Evidence carries diagnostic state (check_specific keys merged flat)
    assert verdict.evidence.model_dump()["matched_target"] == "compass.auth.api_key"
    assert verdict.evidence.model_dump()["span_count"] == 3


@pytest.mark.asyncio
async def test_passes_on_substring_match_for_dynamic_span_names() -> None:
    """A target like 'compass.execution.' matches compass.execution.lookup."""
    c = _criterion(
        criterion_code="execution_dispatched_for_execute_route",
        target_spans=["compass.execution."],
    )
    ev = RecordSpanEvaluator(criterion=c)
    fake_trace = [
        {"span_name": "compass.execution.lookup", "attributes": {}},
    ]
    with (
        patch(
            "compass_backend.quality.criteria.settings.logfire_read_token",
            SecretStr("fake-read-token"),
        ),
        patch(
            "compass_backend.quality.criteria._fetch_span_tree",
            AsyncMock(return_value=fake_trace),
        ),
    ):
        verdict = await ev.evaluate(_ctx())
    assert verdict.outcome == "pass"
    assert verdict.evidence.model_dump()["matched_span_name"] == "compass.execution.lookup"


@pytest.mark.asyncio
async def test_passes_when_any_of_multiple_target_spans_appears() -> None:
    """target_spans is OR-semantics: any entry matching is enough."""
    c = _criterion(
        target_spans=["compass.never.fires", "compass.sse.emit"],
    )
    ev = RecordSpanEvaluator(criterion=c)
    fake_trace = [{"span_name": "compass.sse.emit", "attributes": {}}]
    with (
        patch(
            "compass_backend.quality.criteria.settings.logfire_read_token",
            SecretStr("fake-read-token"),
        ),
        patch(
            "compass_backend.quality.criteria._fetch_span_tree",
            AsyncMock(return_value=fake_trace),
        ),
    ):
        verdict = await ev.evaluate(_ctx())
    assert verdict.outcome == "pass"
    assert verdict.evidence.model_dump()["matched_target"] == "compass.sse.emit"


# ─── 2. target_spans mode — fail path ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_fails_when_no_target_span_name_appears() -> None:
    """outcome='fail' when none of target_spans appears in the trace."""
    c = _criterion(target_spans=["compass.never.fires"])
    ev = RecordSpanEvaluator(criterion=c)
    fake_trace = [
        {"span_name": "compass.turn", "attributes": {}},
        {"span_name": "compass.session.load", "attributes": {}},
    ]
    with (
        patch(
            "compass_backend.quality.criteria.settings.logfire_read_token",
            SecretStr("fake-read-token"),
        ),
        patch(
            "compass_backend.quality.criteria._fetch_span_tree",
            AsyncMock(return_value=fake_trace),
        ),
    ):
        verdict = await ev.evaluate(_ctx())
    assert verdict.outcome == "fail"
    assert "['compass.never.fires']" in verdict.reason
    assert "2 trace spans" in verdict.reason
    assert verdict.evidence.model_dump()["matched_target"] is None


# ─── 3. Error short-circuits ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_when_no_logfire_read_token() -> None:
    """outcome='error' with skip_reason='no_logfire_token' when token is None."""
    c = _criterion(target_spans=["compass.auth.api_key"])
    ev = RecordSpanEvaluator(criterion=c)
    with patch(
        "compass_backend.quality.criteria.settings.logfire_read_token",
        None,
    ):
        verdict = await ev.evaluate(_ctx())
    assert verdict.outcome == "error"
    assert "infra unavailable" in verdict.reason
    assert "LOGFIRE_READ_TOKEN" in verdict.reason
    assert verdict.evidence.model_dump()["skip_reason"] == "no_logfire_token"


@pytest.mark.asyncio
async def test_error_when_no_trace_id() -> None:
    """outcome='error' with skip_reason='no_trace_id' when ctx.trace_id is None."""
    c = _criterion(target_spans=["compass.auth.api_key"])
    ev = RecordSpanEvaluator(criterion=c)
    verdict = await ev.evaluate(_ctx(trace_id=None))
    assert verdict.outcome == "error"
    assert "no trace_id" in verdict.reason
    assert verdict.evidence.model_dump()["skip_reason"] == "no_trace_id"


@pytest.mark.asyncio
async def test_error_when_trace_is_empty() -> None:
    """outcome='error' with skip_reason='empty_trace' when Logfire returns []."""
    c = _criterion(target_spans=["compass.auth.api_key"])
    ev = RecordSpanEvaluator(criterion=c)
    with (
        patch(
            "compass_backend.quality.criteria.settings.logfire_read_token",
            SecretStr("fake-read-token"),
        ),
        patch(
            "compass_backend.quality.criteria._fetch_span_tree",
            AsyncMock(return_value=[]),
        ),
    ):
        verdict = await ev.evaluate(_ctx())
    assert verdict.outcome == "error"
    assert "trace empty" in verdict.reason
    assert verdict.evidence.model_dump()["skip_reason"] == "empty_trace"
    assert verdict.evidence.model_dump()["trace_id"] == "trace-abc-123"


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["sweep_run", "manual_replay"])
async def test_not_applicable_when_trace_empty_in_offline_lane(lane: str) -> None:
    """U3 (a): an empty trace in the sweep/replay lane is NOT_APPLICABLE, not error.

    The offline read token frequently cannot see the trace at evaluation time
    (queued/truncated/wrong scope -- #1325/#1326), so erroring on every execute
    turn buried real signal. The `not applicable:` reason prefix routes the
    trial out of both numerator and denominator
    (quality/scorecard.py::_is_not_applicable). This is NOT a fabricated pass:
    a readable trace whose target span is genuinely absent still fails.
    """
    c = _criterion(target_spans=["compass.auth.api_key"])
    ev = RecordSpanEvaluator(criterion=c)
    with (
        patch(
            "compass_backend.quality.criteria.settings.logfire_read_token",
            SecretStr("fake-read-token"),
        ),
        patch(
            "compass_backend.quality.criteria._fetch_span_tree",
            AsyncMock(return_value=[]),
        ),
    ):
        verdict = await ev.evaluate(_ctx(triggered_by=lane))
    # Outcome stays "error" on the wire (the three-state verdict vocabulary),
    # but the "not applicable:" reason prefix makes the Scorecard exclude it.
    assert verdict.outcome == "error"
    assert verdict.reason.startswith("not applicable:")
    assert "no readable span tree" in verdict.reason
    assert verdict.evidence.model_dump()["skip_reason"] == "empty_trace_offline_lane"
    assert verdict.evidence.model_dump()["triggered_by"] == lane


@pytest.mark.asyncio
async def test_fetch_span_tree_does_not_cap_trace_sql_at_100(monkeypatch) -> None:
    """Regression: late persistence spans must remain visible to span assertions."""

    captured_sql: list[str] = []
    full_trace: list[dict[str, object]] = [
        {"span_name": f"compass.noise.{ordinal}", "attributes": {}}
        for ordinal in range(100)
    ]
    full_trace.append(
        {
            "span_name": "compass.persistence.save_turn",
            "attributes": {"snapshot_present": True},
        }
    )

    class _FakeResponse:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self._rows = rows

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, list[dict[str, object]]]:
            return {"rows": self._rows}

    class _FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            return None

        async def get(
            self,
            _url: str,
            *,
            params: dict[str, str],
            headers: dict[str, str],
        ) -> _FakeResponse:
            sql = params["sql"]
            captured_sql.append(sql)
            if "LIMIT 100" in sql.upper():
                return _FakeResponse(full_trace[:100])
            return _FakeResponse(full_trace)

    monkeypatch.setattr(criteria_module.httpx, "AsyncClient", _FakeClient)

    with patch(
        "compass_backend.quality.criteria.settings.logfire_read_token",
        SecretStr("fake-read-token"),
    ):
        spans = await criteria_module._fetch_span_tree("trace-with-late-persistence")

    assert captured_sql
    assert "LIMIT" not in captured_sql[0].upper()
    assert len(spans) == 101
    assert spans[-1]["span_name"] == "compass.persistence.save_turn"


@pytest.mark.asyncio
async def test_fetch_span_tree_sends_explicit_limit_to_defeat_api_default(
    monkeypatch,
) -> None:
    """Regression for #1325: the /v1/query call must carry an explicit ``limit``.

    The real truncation is *not* a SQL ``LIMIT`` clause (we never emitted one) —
    it is Logfire's server-side default row cap (100 on our deployment) that
    applies when no ``limit`` query parameter is sent. Because the query is
    ``ORDER BY start_timestamp ASC``, that cap keeps the earliest spans and drops
    the late-firing ``compass.persistence.save_turn`` / trailing
    ``compass.execution.*`` spans, so span_assertion criteria fail on any turn
    with >100 spans. This fake models that server cap — it honours an explicit
    ``limit`` and otherwise truncates to 100 — so the test is red without the fix
    and green with it.
    """

    captured_params: list[dict[str, object]] = []
    api_default_cap = 100
    full_trace: list[dict[str, object]] = [
        {"span_name": f"compass.noise.{ordinal}", "attributes": {}}
        for ordinal in range(120)
    ]
    full_trace.append(
        {
            "span_name": "compass.persistence.save_turn",
            "attributes": {"snapshot_present": True},
        }
    )

    class _FakeResponse:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self._rows = rows

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, list[dict[str, object]]]:
            return {"rows": self._rows}

    class _FakeClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            return None

        async def get(
            self,
            _url: str,
            *,
            params: dict[str, object],
            headers: dict[str, str],
        ) -> _FakeResponse:
            captured_params.append(params)
            raw_limit = params.get("limit")
            effective = int(raw_limit) if raw_limit is not None else api_default_cap
            return _FakeResponse(full_trace[:effective])

    monkeypatch.setattr(criteria_module.httpx, "AsyncClient", _FakeClient)

    with patch(
        "compass_backend.quality.criteria.settings.logfire_read_token",
        SecretStr("fake-read-token"),
    ):
        spans = await criteria_module._fetch_span_tree("trace-with-late-persistence")

    assert captured_params, "Logfire query was never issued"
    # An explicit limit must be sent, large enough to clear any single turn —
    # without it the API caps at its default and late spans vanish.
    assert "limit" in captured_params[0], "no explicit row limit sent to Logfire"
    assert int(captured_params[0]["limit"]) >= 1000
    # The late persistence span survives because the whole trace came back.
    assert len(spans) == 121
    assert spans[-1]["span_name"] == "compass.persistence.save_turn"


# ─── 4. Legacy fallback: payload.assertion.tool path ──────────────────────────


@pytest.mark.asyncio
async def test_legacy_tool_assertion_still_passes_when_target_spans_none() -> None:
    """When target_spans is None, falls back to payload.assertion.tool check."""
    c = _criterion(
        criterion_code="find_metric_called_before_writer",
        payload={"assertion": {"tool": "find_metric", "called_before": "writer"}},
        target_spans=None,
    )
    ev = RecordSpanEvaluator(criterion=c)
    # _matches_tool substring-matches on span_name; "find_metric" in name passes
    fake_trace = [{"span_name": "agent.find_metric.run", "attributes": {}}]
    with (
        patch(
            "compass_backend.quality.criteria.settings.logfire_read_token",
            SecretStr("fake-read-token"),
        ),
        patch(
            "compass_backend.quality.criteria._fetch_span_tree",
            AsyncMock(return_value=fake_trace),
        ),
    ):
        verdict = await ev.evaluate(_ctx())
    assert verdict.outcome == "pass"
    assert "[naive: 'tool called at all']" in verdict.reason
    assert "tool was called" in verdict.reason


@pytest.mark.asyncio
async def test_legacy_tool_assertion_fails_when_tool_absent() -> None:
    """When target_spans is None and the tool is not seen, outcome='fail'."""
    c = _criterion(
        payload={"assertion": {"tool": "find_metric", "called_before": "writer"}},
        target_spans=None,
    )
    ev = RecordSpanEvaluator(criterion=c)
    fake_trace = [{"span_name": "compass.turn", "attributes": {}}]
    with (
        patch(
            "compass_backend.quality.criteria.settings.logfire_read_token",
            SecretStr("fake-read-token"),
        ),
        patch(
            "compass_backend.quality.criteria._fetch_span_tree",
            AsyncMock(return_value=fake_trace),
        ),
    ):
        verdict = await ev.evaluate(_ctx())
    assert verdict.outcome == "fail"
    assert "tool was NOT called" in verdict.reason


# ─── 5. CriterionRecord round-trip — repo hydrates target_spans ──────────────


class _FakeRecord:
    """Minimal asyncpg.Record stand-in supporting __getitem__ and keys()."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getitem__(self, key: str):
        return self._data[key]

    def keys(self):
        return self._data.keys()


def _make_row(*, target_spans: list[str] | None) -> _FakeRecord:
    data = {
        "id": 7,
        "criterion_code": "auth_succeeded_for_user_turn",
        "text": "Auth succeeded",
        "category": "process_integrity",
        "check_type": "span_assertion",
        "severity": "warn",
        "priority": 30,
        "is_mandatory_global": True,
        "scenario_ids": [],
        "topic_tags": [],
        "intent_tags": [],
        "payload": {"description": "asserts auth ran"},
        "version_hash": "auth-succeeded-for-user-turn-v1",
        "active": True,
        "target_spans": target_spans,
    }
    return _FakeRecord(data)


def test_criterion_from_row_hydrates_target_spans_when_populated() -> None:
    """_criterion_from_row reads the target_spans column into the model."""
    row = _make_row(target_spans=["compass.auth.api_key"])
    c = _criterion_from_row(row)  # type: ignore[arg-type]
    assert c.target_spans == ["compass.auth.api_key"]
    assert c.check_type == "span_assertion"
    assert c.is_mandatory_global is True


def test_criterion_from_row_target_spans_none_when_column_is_null() -> None:
    """A NULL target_spans column hydrates as None on the model."""
    row = _make_row(target_spans=None)
    c = _criterion_from_row(row)  # type: ignore[arg-type]
    assert c.target_spans is None


def test_criterion_from_row_tolerates_pre_018_schemas_without_target_spans() -> None:
    """If the column is absent entirely (pre-018), hydration still works."""

    class _RecordNoTargetSpans:
        def __init__(self, data: dict) -> None:
            self._data = data

        def __getitem__(self, key: str):
            return self._data[key]

        def keys(self):
            return self._data.keys()

    data = {
        "id": 9,
        "criterion_code": "legacy_criterion",
        "text": "Legacy",
        "category": "process_integrity",
        "check_type": "deterministic",
        "severity": "warn",
        "priority": 10,
        "is_mandatory_global": False,
        "scenario_ids": [],
        "topic_tags": [],
        "intent_tags": [],
        "payload": {"validator_name": "legacy_validator"},
        "version_hash": "legacy-v1",
        "active": True,
        # target_spans intentionally absent — pre-018 schema shape
    }
    c = _criterion_from_row(_RecordNoTargetSpans(data))  # type: ignore[arg-type]
    assert c.target_spans is None
