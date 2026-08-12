"""Tests for the fresh chat SSE compatibility adapter."""

import json
from dataclasses import dataclass, field
from typing import Any

import logfire.testing
import pytest
from fastapi.testclient import TestClient
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from compass_backend.api.app import create_app
from compass_backend.api.sse import chat_response_sse_payloads
from compass_backend.artifacts import (
    ChartArtifact,
    ChartPoint,
    ChartSeries,
    CitationRef,
    CompositeRankingResult,
    CoverageBreakdown,
    CoverageDisclosure,
    CoverageFrame,
    CsvExportArtifact,
    MethodologyRef,
    MetricCountResult,
    MetricLookupResult,
    MetricRankingResult,
    MetricValueRow,
    RankingRow,
    ThresholdCountRow,
)
from compass_backend.contracts import (
    ChatResponse,
    ClarificationOption,
    ClarificationRequest,
    DirectResponse,
    MetricSpec,
    OutputSpec,
    QueryPlan,
    SelectionSpec,
    PlannerTurn,
    ResponseManifest,
    SessionState,
    ValidationFinding,
    ValidationReport,
)
from compass_backend.rendering import render_response
from compass_backend.rendering.chart_visibility import resolve_chart_visibility
from compass_backend.rendering.policy_guidance import (
    POLICY_GUIDANCE_VALIDATION_FAILED_BODY,
)
from compass_backend.session import InMemorySessionStore


# ---------------------------------------------------------------------------
# Minimal SSE parsing helpers (inlined from the deleted comparison_gate runner)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SseCapture:
    """Collected data from a Compass SSE stream."""

    event_types: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    full_text: str = ""
    metric_data: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    chart: dict[str, Any] | None = None
    session_id: str | None = None
    trace_id: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _parse_json_object(data: str) -> dict[str, Any]:
    if not data:
        return {}
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return {"raw": data}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def parse_sse_stream(raw: str) -> SseCapture:
    """Parse a text/event-stream body into a compact capture."""

    events: list[dict[str, Any]] = []
    event_types: list[str] = []
    text_chunks: list[str] = []
    metric_data: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    chart: dict[str, Any] | None = None
    session_id: str | None = None
    trace_id: str | None = None
    error: str | None = None

    current_event: str | None = None
    current_data: list[str] = []

    def flush() -> None:
        nonlocal chart, error, session_id, trace_id
        if current_event is None:
            return
        data_str = "".join(current_data)
        data = _parse_json_object(data_str)
        event_types.append(current_event)
        events.append({"event": current_event, "data": data})
        if current_event == "trace":
            trace_id = _string_or_none(data.get("trace_id"))
            session_id = _string_or_none(data.get("session_id")) or session_id
        elif current_event == "text":
            content = _string_or_none(data.get("content"))
            if content:
                text_chunks.append(content)
        elif current_event == "data":
            rows = data.get("results")
            if isinstance(rows, list):
                metric_data.extend(row for row in rows if isinstance(row, dict))
        elif current_event == "citations":
            rows = data.get("citations")
            if isinstance(rows, list):
                citations.extend(row for row in rows if isinstance(row, dict))
        elif current_event == "chart":
            chart = data
        elif current_event == "done":
            session_id = _string_or_none(data.get("session_id")) or session_id
        elif current_event == "error":
            error = _string_or_none(data.get("error")) or "Unknown error"

    for raw_line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            flush()
            current_event = None
            current_data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            if current_event is not None and current_data:
                flush()
            current_event = line[6:].strip()
            current_data = []
        elif line.startswith("data:"):
            current_data.append(line[5:].strip())
    flush()

    return SseCapture(
        event_types=event_types,
        events=events,
        full_text="".join(text_chunks),
        metric_data=metric_data,
        citations=citations,
        chart=chart,
        session_id=session_id,
        trace_id=trace_id,
        error=error,
    )


def _agent_for_turn(turn: PlannerTurn) -> Agent:
    # call_tools=[] keeps the canned-output TestModel from auto-invoking the
    # always-attached Compass catalog toolset (#1248); the offline test app
    # has no live DB pool for a tool call to reach.
    return Agent(
        TestModel(
            custom_output_args=turn.model_dump(mode="json"),
            call_tools=[],
        ),
        output_type=PlannerTurn,
    )


def _direct_turn() -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=0.98,
        direct_response=DirectResponse(
            message="Hello from Compass.",
            reason="Greeting does not require deterministic execution.",
        ),
    )


_PROGRESS_EVENT_TYPES = {
    "stage_start",
    "stage_end",
    "tool_call_start",
    "tool_call_end",
}


def test_chat_route_defaults_to_sse_for_frontend_proxy() -> None:
    with TestClient(
        create_app(
            planner_agent=_agent_for_turn(_direct_turn()),
            session_store=InMemorySessionStore(),
        )
    ) as client:
        response = client.post("/api/v1/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    capture = parse_sse_stream(response.text)
    # The SSE stream now interleaves advisory progress frames
    # (stage_start/stage_end/tool_call_start/tool_call_end) between
    # `trace` and the terminal block. Filter those out so the assertion
    # describes the durable event vocabulary the frontend consumes.
    legacy_events = [
        kind
        for kind in capture.event_types
        if kind not in _PROGRESS_EVENT_TYPES
    ]
    assert legacy_events == ["trace", "status", "text", "done"]
    assert capture.full_text == "Hello from Compass."
    assert capture.session_id


def test_chat_route_returns_json_when_accept_requests_json() -> None:
    with TestClient(
        create_app(
            planner_agent=_agent_for_turn(_direct_turn()),
            session_store=InMemorySessionStore(),
        )
    ) as client:
        response = client.post(
            "/api/v1/chat",
            json={"message": "hello"},
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["message"] == "Hello from Compass."


def test_chat_simple_route_returns_json_without_accept_negotiation() -> None:
    with TestClient(
        create_app(
            planner_agent=_agent_for_turn(_direct_turn()),
            session_store=InMemorySessionStore(),
        )
    ) as client:
        response = client.post("/api/v1/chat/simple", json={"message": "hello"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["message"] == "Hello from Compass."


def _clarify_turn_with_options() -> PlannerTurn:
    return PlannerTurn(
        route="clarify",
        confidence=0.5,
        clarification=ClarificationRequest(
            question='I found more than one covered district matching "Cleveland County".',
            missing_fields=["district"],
            candidates=["Cleveland County Schools, NC", "Cleveland County, OK"],
            candidate_options=[
                ClarificationOption(
                    value="2700",
                    label="Cleveland County Schools, NC",
                    detail="15,000 students · Shelby",
                ),
                ClarificationOption(value="4011", label="Cleveland County, OK"),
            ],
        ),
    )


def test_sse_emits_options_event_after_text_for_clarify_with_options() -> None:
    response = ChatResponse(
        message="I found more than one covered district matching Cleveland County.",
        turn=_clarify_turn_with_options(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
    )

    events = list(chat_response_sse_payloads(response))
    types = [event["event"] for event in events]

    assert "options" in types
    # Ordering contract: text -> options -> done.
    assert types.index("text") < types.index("options") < types.index("done")

    options_event = next(event for event in events if event["event"] == "options")
    payload = json.loads(options_event["data"])
    assert payload["mode"] == "clarify"
    assert payload["missing_fields"] == ["district"]
    assert [option["value"] for option in payload["options"]] == ["2700", "4011"]
    assert payload["options"][0]["label"] == "Cleveland County Schools, NC"
    assert payload["options"][0]["detail"] == "15,000 students · Shelby"


def test_sse_omits_options_event_for_prose_only_clarify() -> None:
    """A metric clarify (no candidate_options) emits no options event."""
    response = ChatResponse(
        message="Which metric did you mean?",
        turn=PlannerTurn(
            route="clarify",
            confidence=0.5,
            clarification=ClarificationRequest(
                question="Which metric did you mean?",
                missing_fields=["metric"],
                candidates=["starting salary", "benefits"],
            ),
        ),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
    )

    types = [event["event"] for event in chat_response_sse_payloads(response)]
    assert "options" not in types


def test_sse_chart_event_serializes_multi_series_artifact_from_chat_response() -> None:
    result = MetricLookupResult(        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=171,
                metric_name="Minimum annual performance pay bonus, if eligible",
                value="$500",
                display_value="$500",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$500",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=172,
                metric_name="Maximum annual performance pay bonus, if eligible",
                value="$5,000",
                display_value="$5,000",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$5,000",
                coverage_reason="answer_value",
            ),
        ],
        chart=ChartArtifact(
            title="Performance pay bonus range by district",
            x_axis_label="District",
            y_axis_label="Bonus amount",
            show_legend=True,
            series=[
                ChartSeries(
                    label="Minimum annual performance pay bonus, if eligible",
                    points=[ChartPoint(label="Alpha", value=500.0, district_id=1)],
                ),
                ChartSeries(
                    label="Maximum annual performance pay bonus, if eligible",
                    points=[ChartPoint(label="Alpha", value=5000.0, district_id=1)],
                ),
            ],
        ),
        total_considered=2,
        excluded_count=0,
        order_statement="Looked up selected metrics for selected districts.",
        source_notes=[],
    )
    response = ChatResponse(
        message="Compared performance pay bonuses.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=result,
    )

    chart_events = [
        event
        for event in chat_response_sse_payloads(response)
        if event["event"] == "chart"
    ]

    assert len(chart_events) == 1
    assert '"show_legend": true' in chart_events[0]["data"]
    assert "Minimum annual performance pay bonus" in chart_events[0]["data"]


def _ranking_result_chart_stripped() -> MetricRankingResult:
    """A chartable ranking whose chart was stripped by the real visibility gate.

    The validator builds a chart for ≥3 distinct points; routing through
    ``resolve_chart_visibility`` with table intent strips it (setting
    ``chart_suppressed`` so the strip survives later re-validation). This is the
    exact result the orchestrator hands to SSE.
    """

    built = MetricRankingResult(
        rows=[
            RankingRow(
                district_id=index,
                district_name=f"District {index:02d}",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=100_000 - index * 1_000,
                display_value=f"${100_000 - index * 1_000:,}",
                academic_year="2024 - 2025",
                rank=index,
                coverage_state="covered",
                coverage_display=f"${100_000 - index * 1_000:,}",
                coverage_reason="answer_value",
            )
            for index in range(1, 5)
        ],
        total_considered=4,
        excluded_count=0,
        order_statement="Ranked by starting salary, highest to lowest.",
    )
    assert built.chart is not None  # chartable: the validator built one
    decision = resolve_chart_visibility(
        QueryPlan(
            question="Rank districts by starting salary.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting salary")],
            output=OutputSpec(format="table"),
        ),
        built,
    )
    assert decision.suggestion_eligible is True
    return decision.result


def test_sse_omits_chart_event_when_chart_stripped_for_table_intent() -> None:
    """#1240: when the visibility gate stripped the chart (user asked for a
    table, not a chart), SSE must emit no `chart` event even though the data
    was chartable."""

    result = _ranking_result_chart_stripped()
    assert result.chart is None
    response = ChatResponse(
        message="Ranked districts by starting salary.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=result,
    )

    types = [event["event"] for event in chat_response_sse_payloads(response)]

    assert "data" in types  # the table/rows still ship
    assert "chart" not in types


def test_sse_emits_followups_event_when_suggestion_eligible() -> None:
    """#1555: when the orchestrator stamps a chart suggestion onto the manifest
    (chartable data, table intent), SSE emits a `followups` event with the
    exact contract: {"prompts": [...]} after data/chart and before done."""

    result = _ranking_result_chart_stripped()
    manifest = ResponseManifest(
        body="Ranked districts by starting salary.",
        status="rendered",
        result_type="metric_ranking",
        validation_valid=True,
        metadata={"suggested_followups": ["Show me this as a bar chart"]},
    )
    response = ChatResponse(
        message="Ranked districts by starting salary.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=result,
        manifest=manifest,
    )

    events = list(chat_response_sse_payloads(response))
    types = [event["event"] for event in events]

    assert "chart" not in types
    assert "followups" in types
    # Ordering contract: data -> followups -> done.
    assert types.index("data") < types.index("followups") < types.index("done")

    followups_event = next(
        event for event in events if event["event"] == "followups"
    )
    payload = json.loads(followups_event["data"])
    assert payload == {"prompts": ["Show me this as a bar chart"]}


def test_sse_omits_followups_event_when_no_suggestion() -> None:
    """A plain answer with no stamped suggestion emits no `followups` event."""

    manifest = ResponseManifest(
        body="Ranked districts by starting salary.",
        status="rendered",
        result_type="metric_ranking",
        validation_valid=True,
        metadata={"row_count": 4},
    )
    response = ChatResponse(
        message="Ranked districts by starting salary.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=_ranking_result_chart_stripped(),
        manifest=manifest,
    )

    types = [event["event"] for event in chat_response_sse_payloads(response)]
    assert "followups" not in types


def test_sse_followups_event_filters_malformed_prompts() -> None:
    """The `_followup_prompts` defensive filter keeps only non-empty strings, so
    a partly-malformed metadata value emits a clean event rather than a broken
    one (the contract its docstring promises)."""

    manifest = ResponseManifest(
        body="Ranked districts by starting salary.",
        status="rendered",
        result_type="metric_ranking",
        validation_valid=True,
        metadata={"suggested_followups": ["Show me this as a bar chart", "", 123]},
    )
    response = ChatResponse(
        message="Ranked districts by starting salary.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=_ranking_result_chart_stripped(),
        manifest=manifest,
    )

    events = list(chat_response_sse_payloads(response))
    followups_event = next(
        event for event in events if event["event"] == "followups"
    )
    payload = json.loads(followups_event["data"])
    assert payload == {"prompts": ["Show me this as a bar chart"]}


def test_sse_omits_followups_event_when_metadata_value_not_a_list() -> None:
    """A non-list `suggested_followups` value emits nothing, not a broken event."""

    manifest = ResponseManifest(
        body="Ranked districts by starting salary.",
        status="rendered",
        result_type="metric_ranking",
        validation_valid=True,
        metadata={"suggested_followups": "Show me this as a bar chart"},
    )
    response = ChatResponse(
        message="Ranked districts by starting salary.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=_ranking_result_chart_stripped(),
        manifest=manifest,
    )

    types = [event["event"] for event in chat_response_sse_payloads(response)]
    assert "followups" not in types


def test_sse_data_event_serializes_resultset_artifacts_and_metadata() -> None:
    result = MetricLookupResult(        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=171,
                metric_name="Minimum annual performance pay bonus, if eligible",
                value="$500",
                display_value="$500",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$500",
                coverage_reason="answer_value",
                citation_markers=[1],
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha Contract, 2024-2025",
                url="https://example.org/alpha.pdf",
                page_number=2,
                page_ref="p. 2",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=1,
            )
        ],
        chart=ChartArtifact(
            title="ResultSet chart",
            x_axis_label="District",
            y_axis_label="Bonus amount",
            points=[ChartPoint(label="Alpha", value=500.0, district_id=1)],
        ),
        csv_export=CsvExportArtifact(
            columns=["district_id", "sentinel"],
            rows=[{"district_id": 1, "sentinel": "from-resultset"}],
        ),
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up selected metrics for selected districts.",
        source_notes=["Artifact source note."],
        methodology_codes=[MethodologyRef(code="lookup_default_district_order")],
    )
    validation = ValidationReport(
        valid=True,
        dimensions_checked=["surface_consistency"],
        findings=[],
    )
    manifest = ResponseManifest(
        body="Compared performance pay bonuses.",
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={"row_count": 1},
    )
    response = ChatResponse(
        message="Compared performance pay bonuses.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=result,
        validation=validation,
        manifest=manifest,
    )

    events = list(chat_response_sse_payloads(response))
    data_payload = json.loads(
        next(event for event in events if event["event"] == "data")["data"]
    )
    chart_payload = json.loads(
        next(event for event in events if event["event"] == "chart")["data"]
    )
    result_payload = result.model_dump(mode="json")

    assert data_payload["result"] == result_payload
    assert data_payload["rows"] == result_payload["rows"]
    assert data_payload["results"] == result_payload["rows"]
    assert data_payload["csv_export"] == result_payload["csv_export"]
    assert data_payload["source_notes"] == result_payload["source_notes"]
    assert data_payload["result"]["methodology_codes"] == [
        {"code": "lookup_default_district_order", "metadata": {}, "audience": "user"}
    ]
    assert data_payload["validation"] == validation.model_dump(mode="json")
    assert data_payload["manifest"] == manifest.model_dump(mode="json")
    assert chart_payload == result_payload["chart"]


def test_sse_citations_event_covers_body_markers_from_composite_children() -> None:
    """SSE citations must cover every citation marker in the current body.

    Composite ranking children can carry citation refs even when the envelope
    citation list is incomplete. The SSE panel should still be built from the
    current response artifact, not silently omit a body marker.
    """

    child_a = MetricRankingResult(
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=89,
                metric_name="BA starting salary",
                value=50000,
                display_value="$50,000",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$50,000",
                coverage_reason="answer_value",
            )
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha BA Salary Schedule",
                url="https://example.org/alpha-ba.pdf",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked BA salary.",
    )
    child_b = MetricRankingResult(
        rows=[
            RankingRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=96,
                metric_name="MA starting salary",
                value=60000,
                display_value="$60,000",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[2],
                coverage_state="covered",
                coverage_display="$60,000",
                coverage_reason="answer_value",
            )
        ],
        citations=[
            CitationRef(
                marker=2,
                title="Bravo MA Salary Schedule",
                url="https://example.org/bravo-ma.pdf",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked MA salary.",
    )
    result = CompositeRankingResult(
        children=[child_a, child_b],
        citations=[child_a.citations[0]],
        total_considered=2,
        excluded_count=0,
        order_statement="Two separate salary rankings.",
    )
    response = ChatResponse(
        message="BA ranking cites [1]. MA ranking cites [2].",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=result,
    )

    events = list(chat_response_sse_payloads(response))
    citations_payload = json.loads(
        next(event for event in events if event["event"] == "citations")["data"]
    )

    assert [citation["marker"] for citation in citations_payload["citations"]] == [1, 2]
    assert citations_payload["citations"][1]["title"] == "Bravo MA Salary Schedule"


def test_sse_data_event_serializes_coverage_breakdown_disclosures_and_csv_fields() -> None:
    result = MetricRankingResult(        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=50000.0,
                display_value="$50,000",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$50,000",
                coverage_reason="answer_value",
            )
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha Contract, 2024-2025",
                url="https://example.org/alpha.pdf",
                page_number=2,
                page_ref="p. 2",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=1,
            )
        ],
        coverage_frame=CoverageFrame(
            universe_count=2,
            in_scope_count=2,
            addressed_count=1,
            real_data_count=1,
            not_reviewed_count=1,
            out_of_universe_count=0,
            coverage_ratio=0.5,
            sparse=False,
            breakdown=CoverageBreakdown(
                answer_value_count=1,
                stale_recent_answer_count=1,
            ),
        ),
        coverage_disclosures=[
            CoverageDisclosure(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                academic_year="2024 - 2025",
                coverage_state="not_reviewed",
                display=(
                    "NCTQ last reviewed Bravo for Average teacher starting salary "
                    "in 2023 - 2024; the value then was $49,000."
                ),
                reason="stale_recent_answer",
                prior_academic_year="2023 - 2024",
                prior_display_value="$49,000",
            )
        ],
        total_considered=2,
        excluded_count=1,
        order_statement="Ranked by starting salary, highest to lowest.",
    )
    response = ChatResponse(
        message="Ranked current salary values.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=result,
    )

    events = list(chat_response_sse_payloads(response))
    data_payload = json.loads(
        next(event for event in events if event["event"] == "data")["data"]
    )

    assert (
        data_payload["result"]["coverage_frame"]["breakdown"][
            "stale_recent_answer_count"
        ]
        == 1
    )
    assert (
        data_payload["result"]["coverage_disclosures"][0]["reason"]
        == "stale_recent_answer"
    )
    # Stale disclosure rows are filtered from the CSV export. #1435
    # The CSV has only the single ranked row (rows[0]); rows[1] does not exist.
    assert "coverage_reason" in data_payload["csv_export"]["columns"]
    assert "coverage_prior_academic_year" in data_payload["csv_export"]["columns"]
    assert len(data_payload["csv_export"]["rows"]) == 1
    assert data_payload["csv_export"]["rows"][0]["coverage_reason"] == "answer_value"
    # No stale row in the CSV.
    stale_reasons = [
        r["coverage_reason"]
        for r in data_payload["csv_export"]["rows"]
        if r.get("coverage_reason") == "stale_recent_answer"
    ]
    assert stale_reasons == []


def test_sse_data_event_keeps_full_rows_when_text_is_previewed() -> None:
    result = MetricRankingResult(
        rows=[
            RankingRow(
                district_id=index,
                district_name=f"District {index:02d}",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=100_000 - index,
                display_value=f"${100_000 - index:,}",
                academic_year="2024 - 2025",
                rank=index,
                coverage_state="covered",
                coverage_display=f"${100_000 - index:,}",
                coverage_reason="answer_value",
            )
            for index in range(1, 13)
        ],
        total_considered=12,
        excluded_count=0,
        order_statement="Ranked by starting salary, highest to lowest.",
    )
    manifest = render_response(
        QueryPlan(
            question="Rank districts by starting salary.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting salary")],
        ),
        result,
        ValidationReport(valid=True),
    )
    response = ChatResponse(
        message=manifest.body,
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=result,
        manifest=manifest,
    )

    events = list(chat_response_sse_payloads(response))
    data_payload = json.loads(
        next(event for event in events if event["event"] == "data")["data"]
    )

    assert "Showing 10 of 12 ranked rows here." in manifest.body
    assert len(data_payload["result"]["rows"]) == 12
    assert len(data_payload["csv_export"]["rows"]) == 12
    assert data_payload["manifest"]["metadata"]["row_count"] == 12
    assert data_payload["manifest"]["metadata"]["displayed_row_count"] == 10
    assert data_payload["manifest"]["metadata"]["data_limit_source"] == "unbounded"
    assert (
        data_payload["manifest"]["metadata"]["display_limit_source"]
        == "renderer_preview"
    )


def test_sse_does_not_emit_artifact_events_for_validation_failed_manifest() -> None:
    result = MetricRankingResult(
        rows=[
            RankingRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=50000.0,
                display_value="$50,000",
                academic_year="2024 - 2025",
                rank=1,
                coverage_state="covered",
                coverage_display="$50,000",
                coverage_reason="answer_value",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Ranked by starting salary, highest to lowest.",
    )
    response = ChatResponse(
        message="I found a deterministic result, but validation failed before rendering.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=result,
        validation=ValidationReport(
            valid=False,
            findings=[
                ValidationFinding(
                    code="unsupported_metric_plan",
                    message="Validation only supports one primary metric.",
                    dimension="metric",
                )
            ],
        ),
        manifest=ResponseManifest(
            body="I found a deterministic result, but validation failed before rendering.",
            status="validation_failed",
            result_type="metric_ranking",
            validation_valid=False,
        ),
    )

    events = list(chat_response_sse_payloads(response))

    assert "data" not in [event["event"] for event in events]
    assert "chart" not in [event["event"] for event in events]
    assert "citations" not in [event["event"] for event in events]


def test_sse_data_event_serializes_manifest_without_resultset() -> None:
    manifest = ResponseManifest(
        body="NCTQ guidance with citation metadata. [1]",
        status="rendered",
        result_type="policy_guidance",
        validation_valid=True,
        metadata={
            "answer_layer": {
                "mode": "shadow",
                "attempted": True,
                "accepted": False,
                "status": "shadow_generated",
            },
            "citations": [
                {
                    "id": 1,
                    "marker": "[1]",
                    "stable_id": "exemplar:detroit-frontloaded-salary-schedule",
                    "url": "https://teacherquality.nctq.org/contract-database/district/detroit",
                    "title": "Detroit Community School District",
                    "citation_status": "ready",
                    "citation_type": "exemplar",
                    "topic_id": "general-salary",
                    "source_kind": "district_exemplar",
                }
            ]
        },
    )
    response = ChatResponse(
        message="NCTQ guidance with citation metadata. [1]",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=None,
        manifest=manifest,
    )

    events = list(chat_response_sse_payloads(response))
    data_payload = json.loads(
        next(event for event in events if event["event"] == "data")["data"]
    )
    citations_payload = json.loads(
        next(event for event in events if event["event"] == "citations")["data"]
    )

    assert [event["event"] for event in events] == [
        "trace",
        "status",
        "text",
        "data",
        "citations",
        "done",
    ]
    assert "result" not in data_payload
    assert data_payload["result_type"] == "policy_guidance"
    assert data_payload["manifest"] == manifest.model_dump(mode="json")
    assert data_payload["manifest"]["metadata"]["answer_layer"]["status"] == (
        "shadow_generated"
    )
    assert "draft_body" not in data_payload["manifest"]["metadata"]["answer_layer"]
    assert "findings" not in data_payload["manifest"]["metadata"]["answer_layer"]
    assert data_payload["manifest"]["metadata"]["citations"][0]["citation_status"] == "ready"
    assert citations_payload["citations"][0]["id"] == 1
    assert citations_payload["citations"][0]["marker"] == "[1]"
    assert (
        citations_payload["citations"][0]["stable_id"]
        == "exemplar:detroit-frontloaded-salary-schedule"
    )


def test_sse_validation_failed_manifest_does_not_emit_citations() -> None:
    manifest = ResponseManifest(
        body=POLICY_GUIDANCE_VALIDATION_FAILED_BODY,
        status="validation_failed",
        result_type="policy_guidance",
        validation_valid=False,
        warnings=["metadata_citation_not_in_body:marker=[1]"],
        metadata={
            "citations": [
                {
                    "id": 1,
                    "marker": "[1]",
                    "stable_id": "exemplar:detroit-frontloaded-salary-schedule",
                    "url": "https://teacherquality.nctq.org/contract-database/district/detroit",
                    "title": "Detroit Community School District",
                    "citation_status": "ready",
                    "citation_type": "exemplar",
                    "topic_id": "general-salary",
                    "source_kind": "district_exemplar",
                }
            ]
        },
    )
    response = ChatResponse(
        message=POLICY_GUIDANCE_VALIDATION_FAILED_BODY,
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=None,
        manifest=manifest,
    )

    events = list(chat_response_sse_payloads(response))
    text_payload = json.loads(
        next(event for event in events if event["event"] == "text")["data"]
    )

    assert text_payload["content"] == POLICY_GUIDANCE_VALIDATION_FAILED_BODY
    assert "citations" not in [event["event"] for event in events]


def test_sse_covered_universe_count_does_not_emit_invented_citations() -> None:
    result = MetricCountResult(        rows=[
            ThresholdCountRow(
                metric_id=0,
                metric_name="Covered district universe",
                value=2,
                display_value="2 covered districts",
                academic_year="2024 - 2025",
                count=2,
                denominator=2,
                filter_statement="covered district universe",
                qualifying_district_ids=[10, 11],
                source="coverage_state",
                coverage_state="covered",
                coverage_display="2 covered districts",
                coverage_reason="covered_universe_count",
            )
        ],
        citations=[],
        total_considered=2,
        excluded_count=0,
        order_statement="Counted covered districts in the resolved selection.",
        source_notes=[],
        methodology_codes=[MethodologyRef(code="covered_universe_selection_count")],
    )
    response = ChatResponse(
        message="Counted covered districts.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        result=result,
    )

    events = list(chat_response_sse_payloads(response))
    data_payload = json.loads(
        next(event for event in events if event["event"] == "data")["data"]
    )

    assert "citations" not in [event["event"] for event in events]
    assert data_payload["result"]["citations"] == []
    assert data_payload["result"]["rows"][0]["citation_markers"] == []


def test_sse_trace_and_done_events_include_trace_and_message_ids() -> None:
    response = ChatResponse(
        message="Hello from Compass.",
        turn=_direct_turn(),
        session=SessionState(session_id="session-1", turn_count=1),
        snapshot_id="snapshot-1",
        trace_id="trace-123",
        logfire_url=(
            "https://logfire-us.pydantic.dev/murmuration/nctqai/live"
            "?traceId=trace-123"
        ),
        message_ids=[22],
    )

    events = list(chat_response_sse_payloads(response))
    trace = next(event for event in events if event["event"] == "trace")
    done = next(event for event in events if event["event"] == "done")

    assert '"trace_id": "trace-123"' in trace["data"]
    assert '"trace_id": "trace-123"' in done["data"]
    assert '"message_ids": [22]' in done["data"]


def test_sse_emit_span_records_session_trace_and_event_aggregates(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compass.sse.emit carries session/trace identifiers + emission aggregates.

    Locks in the Tier 2.1 audit fix: SSE event emission must be observable so
    we can answer "did the backend emit `done`?" from a Logfire trace alone.
    """
    from pydantic import SecretStr

    from compass_backend.config import settings

    monkeypatch.setattr(
        settings,
        "logfire_token",
        SecretStr("pylf_v1_us_real_token_for_sse_test"),
    )

    result = MetricLookupResult(        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=171,
                metric_name="Performance pay bonus",
                value="$500",
                display_value="$500",
                academic_year="2024 - 2025",
                coverage_state="covered",
                coverage_display="$500",
                coverage_reason="answer_value",
                citation_markers=[1],
            ),
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Alpha Contract",
                url="https://example.org/alpha.pdf",
                page_number=2,
                page_ref="p. 2",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=1,
            ),
        ],
        chart=ChartArtifact(
            title="Bonus chart",
            x_axis_label="District",
            y_axis_label="Bonus amount",
            points=[ChartPoint(label="Alpha", value=500.0, district_id=1)],
        ),
        csv_export=None,
        total_considered=1,
        excluded_count=0,
        order_statement="Looked up.",
        source_notes=[],
    )
    response = ChatResponse(
        message="Done.",
        turn=_direct_turn(),
        session=SessionState(session_id="sess-X", turn_count=1),
        snapshot_id="snap-X",
        trace_id="trace-X",
        result=result,
    )

    # Exhaust the generator so the span closes and is exported.
    list(chat_response_sse_payloads(response))

    sse_spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.sse.emit"
    ]
    assert len(sse_spans) == 1
    attrs = sse_spans[0]["attributes"]
    # session/trace/snapshot ids are passed through Logfire's scrubber. In
    # production the compass_logfire_scrubbing_callback preserves them; the
    # capfire test exporter uses default scrubbing which redacts them. Either
    # way, the key must be present — that's what proves wiring is correct.
    assert "session_id" in attrs
    assert "trace_id" in attrs
    assert "snapshot_id" in attrs
    # These attributes aren't sensitive-keyword matches, so values pass through.
    assert attrs["result_type"] == "metric_lookup"
    assert attrs["had_data"] is True
    assert attrs["had_citations"] is True
    assert attrs["had_chart"] is True
    # trace, status, text, data, citations, chart, done = 7 events
    assert attrs["event_count"] == 7
    # OTel serializes list attributes as JSON strings on the wire.
    event_types_attr = attrs["event_types"]
    event_types = (
        json.loads(event_types_attr)
        if isinstance(event_types_attr, str)
        else list(event_types_attr)
    )
    assert event_types == ["trace", "status", "text", "data", "citations", "chart", "done"]
