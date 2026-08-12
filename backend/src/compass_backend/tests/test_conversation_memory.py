"""Tests for backend-only Compass conversation memory."""

from __future__ import annotations

from compass_backend.artifacts import (
    CompositeRankingResult,
    CoverageFrame,
    MetricRankingResult,
    RankingRow,
    ResultSet,
)
from compass_backend.contracts import (
    ClarificationOption,
    ClarificationRequest,
    ConversationMemory,
    ConversationSummary,
    MetricSpec,
    PendingQueryContext,
    PlannerTurn,
    PolicyGuidancePlan,
    QueryContext,
    QueryPlan,
    ResponseManifest,
    SelectionSpec,
    SessionState,
    TurnSnapshot,
)
from compass_backend.session.memory import (
    build_policy_guidance_context,
    build_result_memory_ref,
    conversation_memory_after_turn,
)


def _result() -> ResultSet:
    return MetricRankingResult(
        rows=[
            RankingRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=89,
                metric_name="Starting salary",
                value=70000.0,
                display_value="$70,000",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[],
                coverage_state="covered",
                coverage_display="$70,000",
                coverage_reason="answer_value",
            )
        ],
        total_considered=1,
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=1,
            in_scope_count=1,
            addressed_count=1,
            real_data_count=1,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        order_statement="Ranked by starting salary, highest to lowest.",
        source_notes=[],
    )


def _composite_result() -> CompositeRankingResult:
    ba_child = _result()
    ma_rows = [
        row.model_copy(
            update={
                "metric_id": 90,
                "metric_name": "MA starting salary",
                "value": 76000.0,
                "display_value": "$76,000",
            }
        )
        for row in ba_child.rows
    ]
    ma_child = ba_child.model_copy(
        update={
            "rows": ma_rows,
            "order_statement": "Ranked by MA starting salary, highest to lowest.",
        }
    )
    return CompositeRankingResult(
        children=[ba_child, ma_child],
        total_considered=0,
        excluded_count=0,
        order_statement="2 metrics, ranked highest to lowest by each.",
    )


def _executed_snapshot() -> TurnSnapshot:
    plan = QueryPlan(
        question="Rank California starting salaries.",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="starting salary")],
    )
    query_context = QueryContext(
        query_plan=plan,
        result_type="metric_ranking",
        row_count=1,
    )
    return TurnSnapshot(
        snapshot_id="snapshot-1",
        session_id="session-1",
        turn_index=1,
        user_message="Rank California starting salaries.",
        assistant_message="Bravo is first.",
        planner_turn=PlannerTurn(route="execute", confidence=0.9, query_plan=plan),
        query_context=query_context,
    )


def _policy_guidance_snapshot() -> TurnSnapshot:
    plan = PolicyGuidancePlan(
        topic_ids=["differentiated-pay"],
        layers=["exemplars"],
        intent_summary="User asked for performance pay exemplars.",
        focus_terms=["performance pay"],
    )
    return TurnSnapshot(
        snapshot_id="guidance-snapshot-1",
        session_id="session-1",
        turn_index=1,
        user_message="Show strong performance pay exemplars.",
        assistant_message="District of Columbia Public Schools is an exemplar.",
        planner_turn=PlannerTurn(
            route="policy_guidance",
            confidence=0.9,
            policy_guidance=plan,
        ),
    )


def _policy_guidance_manifest() -> ResponseManifest:
    return ResponseManifest(
        body=(
            "Here are NCTQ-curated exemplary district policies for Performance Pay.\n\n"
            "- **District of Columbia Public Schools** — DCPS links additional "
            "compensation to teacher performance. [1]"
        ),
        status="rendered",
        result_type="policy_guidance",
        validation_valid=True,
        metadata={
            "topic_ids": ["differentiated-pay"],
            "layers": ["exemplars"],
            "primary_topic_id": None,
            "intent_summary": "User asked for performance pay exemplars.",
            "focus_terms": ["performance pay"],
            "citations": [
                {
                    "id": 1,
                    "marker": "[1]",
                    "stable_id": "exemplar:dcps-performance-pay",
                    "citation_type": "exemplar",
                    "topic_id": "differentiated-pay",
                    "district": "District of Columbia Public Schools",
                    "district_id": 150,
                    "subtopic": "Performance pay",
                    "source_kind": "district_exemplar",
                    "citation_status": "ready",
                    "url": "https://teacherquality.nctq.org/contract-database/district/dcps",
                    "title": "District of Columbia Public Schools — Performance pay",
                }
            ],
        },
    )


def test_turn_snapshot_accepts_legacy_payload_without_memory() -> None:
    snapshot = TurnSnapshot.model_validate(
        {
            "snapshot_id": "legacy-snapshot",
            "session_id": "session-1",
            "turn_index": 1,
            "user_message": "Hello",
            "assistant_message": "Hi",
            "planner_turn": {
                "route": "direct",
                "confidence": 0.9,
                "direct_response": {"message": "Hi", "reason": "Greeting"},
            },
        }
    )

    assert snapshot.memory is None
    assert snapshot.result_refs == []
    assert snapshot.result is None


def test_legacy_snapshot_payload_is_folded_into_memory_envelope() -> None:
    snapshot = _executed_snapshot()
    payload = snapshot.model_dump(mode="json")
    payload["conversation_memory"] = {
        "summary": "User is comparing salaries.",
        "active_user_goal": "Compare salary policies.",
        "latest_turn_index": 1,
        "source_snapshot_ids": ["snapshot-1"],
    }
    payload["result_memory_refs"] = [
        build_result_memory_ref(snapshot, _result()).model_dump(mode="json")
    ]

    restored = TurnSnapshot.model_validate(payload)
    persisted = restored.model_dump(mode="json")

    assert restored.memory is not None
    assert restored.memory.latest_query_context == snapshot.query_context
    assert restored.memory.summary is not None
    assert restored.memory.summary.summary == "User is comparing salaries."
    assert restored.memory.result_refs[0].snapshot_id == "snapshot-1"
    assert "query_context" not in persisted
    assert "conversation_memory" not in persisted
    assert "result_memory_refs" not in persisted
    assert "memory" in persisted


def test_session_state_memory_contracts_are_one_internal_envelope() -> None:
    summary = ConversationSummary(
        summary="User is comparing salaries.",
        active_user_goal="Compare salary policies.",
    )
    query_context = _executed_snapshot().query_context
    state = SessionState(
        memory=ConversationMemory(
            summary=summary,
            latest_query_context=query_context,
            result_refs=[build_result_memory_ref(_executed_snapshot(), _result())],
            latest_turn_index=1,
            source_snapshot_ids=["snapshot-1"],
        )
    )

    public = state.model_dump(mode="json")

    assert "memory" not in public
    assert "query_context" not in SessionState.model_fields
    assert "pending_query_context" not in SessionState.model_fields
    assert "conversation_memory" not in SessionState.model_fields
    assert "result_memory_refs" not in SessionState.model_fields
    assert "recent_transcript" not in SessionState.model_fields
    assert state.query_context == query_context
    assert state.conversation_summary == summary


def test_old_memory_alias_accessors_are_not_public_runtime_api() -> None:
    state = SessionState(memory=ConversationMemory())
    snapshot = _executed_snapshot().model_copy(update={"memory": ConversationMemory()})

    assert not hasattr(state, "conversation_memory")
    assert not hasattr(snapshot, "conversation_memory")


def test_turn_snapshot_persists_memory_and_result_artifact() -> None:
    summary = ConversationSummary(
        summary="User is comparing salaries.",
        active_user_goal="Compare salary policies.",
    )
    memory = ConversationMemory(
        summary=summary,
        latest_turn_index=1,
        source_snapshot_ids=["snapshot-1"],
    )
    snapshot = _executed_snapshot().model_copy(
        update={"memory": memory, "result": _result()}
    )

    persisted = snapshot.model_dump(mode="json")

    assert persisted["memory"]["summary"]["summary"] == "User is comparing salaries."
    assert persisted["result"]["result_type"] == "metric_ranking"


def test_build_result_memory_ref_summarizes_valid_result() -> None:
    snapshot = _executed_snapshot()

    ref = build_result_memory_ref(snapshot, _result())

    assert ref.snapshot_id == "snapshot-1"
    assert ref.question == "Rank California starting salaries."
    assert ref.result_type == "metric_ranking"
    assert ref.row_count == 1
    assert ref.displayed_row_count == 1
    assert ref.metrics[0].metric_id == 89
    assert ref.districts[0].district_name == "Bravo"
    # Charts are suppressed for single-row results (fewer than 3 points)
    assert ref.has_chart is False
    assert ref.has_csv_export is True
    assert "Starting salary" in ref.digest


def test_build_result_memory_ref_summarizes_composite_child_rows() -> None:
    snapshot = _executed_snapshot()

    ref = build_result_memory_ref(snapshot, _composite_result())

    assert ref.result_type == "composite_ranking"
    assert ref.row_count == 2
    assert [metric.metric_name for metric in ref.metrics] == [
        "Starting salary",
        "MA starting salary",
    ]
    assert ref.districts[0].district_name == "Bravo"
    assert "2 composite_ranking rows" in ref.digest


def test_conversation_memory_after_turn_preserves_clarification_options() -> None:
    snapshot = TurnSnapshot(
        snapshot_id="snapshot-2",
        session_id="session-1",
        turn_index=2,
        user_message="No, try the salary one as a chart.",
        assistant_message="Which salary metric should I use?",
        planner_turn=PlannerTurn(
            route="clarify",
            confidence=0.7,
            clarification=ClarificationRequest(
                question="Which salary metric should I use?",
                missing_fields=["metric"],
                candidates=["Starting salary", "Maximum salary"],
                pending_context=PendingQueryContext(
                    metrics=[MetricSpec(name="salary")],
                    missing_fields=["metric"],
                ),
            ),
        ),
    )
    previous = ConversationMemory(
        summary=ConversationSummary(
            summary="User is comparing district salary policies.",
            active_user_goal="Compare salary policies.",
        ),
        latest_turn_index=1,
        source_snapshot_ids=["snapshot-1"],
    )

    memory = conversation_memory_after_turn(previous, snapshot)

    assert memory.summary is not None
    assert memory.summary.summary == previous.summary.summary
    assert memory.summary.active_user_goal == "No, try the salary one as a chart."
    assert memory.summary.open_questions == ["Which salary metric should I use?"]
    assert memory.summary.last_clarification_options == [
        "Starting salary",
        "Maximum salary",
    ]
    assert "Prefers chartable results" in memory.summary.user_preferences
    assert "try the salary one as a chart" in memory.summary.rejected_choices[0]


def test_conversation_memory_persists_structured_clarification_options() -> None:
    """#1348: a district clarify turn's grounded options persist for next-turn resume."""
    snapshot = TurnSnapshot(
        snapshot_id="snapshot-3",
        session_id="session-1",
        turn_index=2,
        user_message="What are evaluation policies in Cleveland County?",
        assistant_message="Which district do you mean?",
        planner_turn=PlannerTurn(
            route="clarify",
            confidence=0.6,
            clarification=ClarificationRequest(
                question="Which district do you mean?",
                missing_fields=["district"],
                candidates=["Cleveland County Schools, NC", "Cleveland County, OK"],
                candidate_options=[
                    ClarificationOption(value="2700", label="Cleveland County Schools, NC"),
                    ClarificationOption(value="4011", label="Cleveland County, OK"),
                ],
            ),
        ),
    )

    memory = conversation_memory_after_turn(None, snapshot)

    assert [o.value for o in memory.pending_clarification_options] == ["2700", "4011"]
    assert memory.pending_clarification_options[0].label == "Cleveland County Schools, NC"


def test_conversation_memory_clears_clarification_options_on_execute() -> None:
    """Once the turn executes, stale options must not linger for a later click."""
    previous = ConversationMemory(
        pending_clarification_options=[
            ClarificationOption(value="2700", label="Cleveland County Schools, NC"),
        ],
    )

    memory = conversation_memory_after_turn(previous, _executed_snapshot())

    assert memory.pending_clarification_options == []


def test_policy_guidance_memory_stores_rendered_exemplar_refs() -> None:
    snapshot = _policy_guidance_snapshot()
    guidance_context = build_policy_guidance_context(
        snapshot,
        _policy_guidance_manifest(),
    )

    memory = conversation_memory_after_turn(
        None,
        snapshot,
        latest_policy_guidance_context=guidance_context,
    )

    assert memory.latest_query_context is None
    assert memory.latest_policy_guidance_context is not None
    assert memory.latest_policy_guidance_context.topic_ids == ["differentiated-pay"]
    assert memory.latest_policy_guidance_context.layers == ["exemplars"]
    assert memory.latest_policy_guidance_context.focus_terms == ["performance pay"]
    assert [ref.exemplar_id for ref in memory.latest_policy_guidance_context.exemplars] == [
        "exemplar:dcps-performance-pay"
    ]
    exemplar = memory.latest_policy_guidance_context.exemplars[0]
    assert exemplar.district == "District of Columbia Public Schools"
    assert exemplar.district_id == 150
    assert exemplar.subtopic == "Performance pay"
    assert exemplar.source_url == (
        "https://teacherquality.nctq.org/contract-database/district/dcps"
    )
    assert exemplar.citation_status == "ready"
    assert exemplar.citation_title == (
        "District of Columbia Public Schools — Performance pay"
    )


def test_successful_execute_turn_clears_policy_guidance_context() -> None:
    guidance_context = build_policy_guidance_context(
        _policy_guidance_snapshot(),
        _policy_guidance_manifest(),
    )
    previous = ConversationMemory(
        latest_policy_guidance_context=guidance_context,
        latest_turn_index=1,
        source_snapshot_ids=["guidance-snapshot-1"],
    )
    execute_snapshot = _executed_snapshot()

    memory = conversation_memory_after_turn(
        previous,
        execute_snapshot,
        latest_query_context=execute_snapshot.query_context,
    )

    assert memory.latest_query_context == execute_snapshot.query_context
    assert memory.latest_policy_guidance_context is None


# ─── Result-memory cap (Change 1 of M3 fix series) ────────────────────────────


def _ranking_result_with_n_districts(n: int) -> ResultSet:
    """Build a MetricRankingResult with N distinct ranked rows."""
    rows = [
        RankingRow(
            district_id=i,
            district_name=f"District {i:03d}",
            state="CA",
            metric_id=89,
            metric_name="Starting salary",
            value=float(50_000 + i),
            display_value=f"${50_000 + i:,}",
            academic_year="2024 - 2025",
            rank=i,
            citation_markers=[],
            coverage_state="covered",
            coverage_display=f"${50_000 + i:,}",
            coverage_reason="answer_value",
        )
        for i in range(1, n + 1)
    ]
    return MetricRankingResult(
        rows=rows,
        total_considered=n,
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=n,
            in_scope_count=n,
            addressed_count=n,
            real_data_count=n,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        order_statement="Ranked by Starting salary, highest to lowest.",
        citations=[],
    )


def test_query_context_caps_district_set_at_max_refs() -> None:
    """Result districts are capped at the documented _MAX_CONTEXT_RESULT_REFS."""
    from compass_backend.orchestration.result_diagnostics import (
        _MAX_CONTEXT_RESULT_REFS,
        _query_context_for_result,
    )

    result = _ranking_result_with_n_districts(_MAX_CONTEXT_RESULT_REFS + 5)
    plan = QueryPlan(
        question="Rank covered districts by starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
    )

    context = _query_context_for_result(query_plan=plan, authority=None, result=result)

    assert len(context.result_districts) == _MAX_CONTEXT_RESULT_REFS
