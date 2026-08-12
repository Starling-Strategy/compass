"""Orchestration seam tests for the Compass answer layer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import compass_backend.orchestration.chat as chat_module
from compass_backend.answer_layer.service import improve_answer
from compass_backend.artifacts import (
    CoverageFrame,
    MetricLookupResult,
    MetricValueRow,
    ResultSelection,
    SelectedDistrict,
)
from compass_backend.config import Settings
from compass_backend.contracts import ChatRequest
from compass_backend.contracts.answer_layer import AnswerDraft, AnswerLayerReport
from compass_backend.contracts.planning import (
    MetricSpec,
    PlannerTurn,
    PolicyGuidancePlan,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.contracts.rendering import ResponseManifest
from compass_backend.contracts.session import SessionState
from compass_backend.contracts.validation import ValidationAuthority, ValidationReport
from compass_backend.execution import ExecutionSuccess
from compass_backend.rendering.chart_visibility import ChartVisibilityDecision


DETERMINISTIC_BODY = "Denver starting salary is $55,000 for 2024-2025. [1]"
IMPROVED_BODY = "Compass can show Denver starting salary: $55,000 for 2024-2025. [1]"


class CaptureSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


def _settings(mode: str) -> Settings:
    return Settings(
        session_store_backend="memory",
        answer_layer_mode=mode,  # type: ignore[arg-type]
        answer_layer_result_types="metric_lookup,policy_guidance",
    )


def _query_plan() -> QueryPlan:
    return QueryPlan(
        operation="lookup",
        question="What is Denver's starting salary?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Denver Public Schools"],
        ),
        metrics=[MetricSpec(name="Average teacher starting salary")],
    )


def _result() -> MetricLookupResult:
    return MetricLookupResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(
                    district_id=26,
                    district_name="Denver Public Schools",
                    state="CO",
                )
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=26,
                district_name="Denver Public Schools",
                state="CO",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=55000.0,
                display_value="$55,000",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$55,000",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up selected metrics for selected districts.",
        coverage_frame=CoverageFrame(
            universe_count=1,
            in_scope_count=1,
            addressed_count=1,
            real_data_count=1,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
    )


def _execute_context(mode: str) -> chat_module._TurnContext:
    return chat_module._TurnContext(
        request=ChatRequest(message="What is Denver's starting salary?"),
        app_settings=_settings(mode),
        auth_user=None,
        trace_id="trace-1",
        session_state=SessionState(session_id="session-1"),
        turn=PlannerTurn(route="execute", confidence=0.91, query_plan=_query_plan()),
    )


def _install_render_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    validation = ValidationReport(valid=True, dimensions_checked=[], findings=[])

    def fake_validate_result(*args, **kwargs):
        return validation

    def fake_render_response(*args, **kwargs):
        return ResponseManifest(
            body=DETERMINISTIC_BODY,
            status="rendered",
            result_type="metric_lookup",
            validation_valid=True,
            metadata={"question": "What is Denver's starting salary?"},
        )

    monkeypatch.setattr(chat_module, "validate_result", fake_validate_result)
    monkeypatch.setattr(chat_module, "render_response", fake_render_response)
    monkeypatch.setattr(chat_module, "_query_context_for_result", lambda **kwargs: None)


@pytest.mark.anyio
async def test_execute_off_mode_preserves_manifest_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_render_stubs(monkeypatch)
    ctx = _execute_context("off")
    span = CaptureSpan()

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("answer layer should not run in off mode")

    monkeypatch.setattr(chat_module, "improve_answer", fail_if_called, raising=False)

    await chat_module._validate_and_render_success(
        ctx,
        ExecutionSuccess(
            result=_result(),
            authority=ValidationAuthority(),
            message="executor body",
        ),
        span,
    )

    assert ctx.message == DETERMINISTIC_BODY
    assert ctx.manifest is not None
    assert ctx.manifest.body == DETERMINISTIC_BODY
    assert "answer_layer" not in ctx.manifest.metadata
    assert span.attributes["answer_layer_mode"] == "off"
    assert span.attributes["answer_layer_status"] == "disabled"


@pytest.mark.anyio
async def test_execute_shadow_mode_adds_metadata_without_replacing_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_render_stubs(monkeypatch)
    ctx = _execute_context("shadow")
    span = CaptureSpan()

    async def fake_improve_answer(manifest, **kwargs):
        return (
            manifest.body,
            AnswerLayerReport(
                mode="shadow",
                attempted=True,
                accepted=False,
                status="shadow_generated",
                draft_body=IMPROVED_BODY,
            ),
        )

    monkeypatch.setattr(chat_module, "improve_answer", fake_improve_answer, raising=False)

    await chat_module._validate_and_render_success(
        ctx,
        ExecutionSuccess(
            result=_result(),
            authority=ValidationAuthority(),
            message="executor body",
        ),
        span,
    )

    assert ctx.message == DETERMINISTIC_BODY
    assert ctx.manifest is not None
    assert ctx.manifest.body == DETERMINISTIC_BODY
    assert ctx.manifest.metadata["answer_layer"] == {
        "mode": "shadow",
        "attempted": True,
        "accepted": False,
        "status": "shadow_generated",
    }
    assert "draft_body" not in ctx.manifest.metadata["answer_layer"]
    assert span.attributes["answer_layer_mode"] == "shadow"
    assert span.attributes["answer_layer_status"] == "shadow_generated"
    assert span.attributes["answer_layer_accepted"] is False
    assert span.attributes["answer_layer_draft_body"] == IMPROVED_BODY


@pytest.mark.anyio
async def test_execute_gated_mode_can_update_manifest_and_message_when_service_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_render_stubs(monkeypatch)
    ctx = _execute_context("gated")

    async def fake_improve_answer(manifest, **kwargs):
        return (
            IMPROVED_BODY,
            AnswerLayerReport(
                mode="gated",
                attempted=True,
                accepted=True,
                status="accepted",
                draft_body=IMPROVED_BODY,
            ),
        )

    monkeypatch.setattr(chat_module, "improve_answer", fake_improve_answer, raising=False)

    await chat_module._validate_and_render_success(
        ctx,
        ExecutionSuccess(
            result=_result(),
            authority=ValidationAuthority(),
            message="executor body",
        ),
        CaptureSpan(),
    )

    assert ctx.message == IMPROVED_BODY
    assert ctx.manifest is not None
    assert ctx.manifest.body == IMPROVED_BODY
    assert ctx.manifest.metadata["answer_layer"]["accepted"] is True


@pytest.mark.anyio
async def test_execute_gated_mode_keeps_deterministic_body_for_real_semantic_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_render_stubs(monkeypatch)
    ctx = _execute_context("gated")

    await chat_module._validate_and_render_success(
        ctx,
        ExecutionSuccess(
            result=_result(),
            authority=ValidationAuthority(),
            message="executor body",
        ),
        CaptureSpan(),
    )

    assert ctx.message == DETERMINISTIC_BODY
    assert ctx.manifest is not None
    assert ctx.manifest.body == DETERMINISTIC_BODY


@pytest.mark.anyio
async def test_policy_guidance_validation_failed_manifest_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = chat_module._TurnContext(
        request=ChatRequest(message="Show me performance-pay guidance."),
        app_settings=_settings("gated"),
        auth_user=None,
        trace_id="trace-1",
        session_state=SessionState(session_id="session-1"),
        turn=PlannerTurn(
            route="policy_guidance",
            confidence=0.9,
            policy_guidance=PolicyGuidancePlan(
                topic_ids=["performance-pay"],
                layers=["exemplars"],
                intent_summary="User asked for policy guidance.",
            ),
        ),
    )

    class FakeLibrary:
        def assemble(self, **kwargs):
            return SimpleNamespace(stances=[], rationales=[], exemplars=[])

    monkeypatch.setattr(chat_module, "get_library", lambda: FakeLibrary())
    monkeypatch.setattr(
        chat_module,
        "render_policy_guidance",
        lambda *args, **kwargs: ResponseManifest(
            body="Unsafe policy guidance body",
            status="validation_failed",
            result_type="policy_guidance",
            validation_valid=False,
        ),
    )

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("answer layer should not run on validation_failed")

    monkeypatch.setattr(chat_module, "improve_answer", fail_if_called, raising=False)

    await chat_module._render_policy_guidance_for_turn(ctx, CaptureSpan())

    assert ctx.message == chat_module.POLICY_GUIDANCE_VALIDATION_FAILED_BODY
    assert ctx.manifest is not None
    assert ctx.manifest.status == "validation_failed"
    assert "answer_layer" not in ctx.manifest.metadata


class _FakeStylist:
    def __init__(self, body: str) -> None:
        self._body = body
        self.last_prompt: str | None = None

    async def run(self, prompt: str):
        self.last_prompt = prompt
        body = self._body

        class _Result:
            output = AnswerDraft(body=body)

        return _Result()


@pytest.mark.anyio
async def test_improve_answer_forwards_allowed_nctq_context() -> None:
    """End-to-end check that allowed_nctq_context flows through the answer layer."""

    manifest = ResponseManifest(
        body=(
            "Denver one-row lookup.\n\n"
            "| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |"
        ),
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={"question": "Denver salary?", "artifact_id": "artifact-1"},
    )
    stylist = _FakeStylist(
        body=(
            "Denver pays $55,000 to start. NCTQ recommends competitive pay "
            "(https://www.nctq.org/district-policy-pathfinder/).\n\n"
            "| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |"
        )
    )

    body, report = await improve_answer(
        manifest,
        mode="gated",
        allowed_result_types="metric_lookup",
        route="execute",
        allowed_nctq_context=(
            "[rationale] Competitive Pay — Districts should pay competitively.\n"
            "URL: https://www.nctq.org/district-policy-pathfinder/",
        ),
        stylist_agent=stylist,
    )

    assert report.accepted is True
    assert "NCTQ recommends" in body
    assert "https://www.nctq.org/district-policy-pathfinder/" in body
    # The stylist prompt must include the snippet so the model can ground its claim.
    assert stylist.last_prompt is not None
    assert "Competitive Pay" in stylist.last_prompt


@pytest.mark.anyio
async def test_improve_answer_falls_back_when_no_context_provided() -> None:
    """Belt-and-suspenders: empty allowed_nctq_context preserves behavior."""
    manifest = ResponseManifest(
        body=(
            "Denver one-row lookup.\n\n"
            "| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |"
        ),
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={"question": "Denver salary?", "artifact_id": "artifact-2"},
    )

    class _PlainStylist:
        async def run(self, prompt: str):
            class _Result:
                output = AnswerDraft(
                    body=(
                        "Denver pays $55,000 to start.\n\n"
                        "| District | Salary |\n| --- | ---: |\n| Denver | $55,000 |"
                    )
                )
            return _Result()

    body, report = await improve_answer(
        manifest,
        mode="gated",
        allowed_result_types="metric_lookup",
        route="execute",
        allowed_nctq_context=(),
        stylist_agent=_PlainStylist(),
    )

    # With no context provided, behavior should match the pre-voice-2.0 path.
    assert report.accepted is True
    assert "Denver pays $55,000" in body


def _chart_unavailable_context(
    mode: str, result_type: str
) -> chat_module._TurnContext:
    """A turn context whose rendered manifest carries the given result_type,
    for exercising the chart_unavailable fallback gate directly."""
    ctx = _execute_context(mode)
    ctx.manifest = ResponseManifest(
        body=DETERMINISTIC_BODY,
        status="rendered",
        result_type=result_type,  # type: ignore[arg-type]
        validation_valid=True,
        metadata={"question": "show me a chart"},
    )
    return ctx


@pytest.mark.parametrize(
    ("mode", "result_type"),
    [
        ("off", "metric_lookup"),
        ("shadow", "metric_lookup"),
        # gated + a chart-capable type the allowlist skips (stylist never runs).
        ("gated", "metric_trend"),
        # gated + an allowed type: the case the fix changes. On main this dropped
        # the line ("the stylist will narrate it") — but a gated draft is often
        # rejected or times out and falls back to THIS deterministic body, losing
        # the note (the R7 report). The note must be here regardless.
        ("gated", "metric_lookup"),
    ],
)
def test_chart_unavailable_note_always_in_deterministic_body(
    mode: str, result_type: str
) -> None:
    """The 'couldn't draw a chart' note ALWAYS lives in the deterministic body
    when chart_unavailable, independent of answer-layer mode/result_type (#1240,
    spec §"never silently lost"). When the answer layer serves a draft it rewrites
    this body (see improve_answer), restating the fact in its own voice — so the
    canned line is replaced, never duplicated."""
    ctx = _chart_unavailable_context(mode, result_type)
    decision = ChartVisibilityDecision(result=_result(), chart_unavailable=True)

    chat_module._attach_chart_visibility_metadata(ctx, decision)

    assert ctx.manifest is not None
    # The structured signal always rides through for the stylist/brief.
    assert ctx.manifest.metadata.get("chart_unavailable") is True
    # And the deterministic body always carries the note — never silently lost.
    assert chat_module.CHART_UNAVAILABLE_FALLBACK_LINE in ctx.manifest.body
