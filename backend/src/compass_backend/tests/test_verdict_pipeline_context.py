"""Tests for verdict_pipeline._build_context — the L2 snapshot builder.

The snapshot is what every L2 validator reads. Phase E2 added two new keys:
  * `state` on each row dict in `artifact_snapshot['table']`
  * `selection_expected = {scope, states, districts}` from QueryPlan.selection

This file locks in the contract that those fields are populated when the
turn carries a QueryPlan, and absent on direct-response turns. Without these
fields, the selection_set_matches_request validator would silently pass
every named-district scenario.

Run with:
    PYDANTIC_AI_GATEWAY_API_KEY=test-key PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_verdict_pipeline_context.py --tb=short
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from compass_backend.artifacts.citations import CitationRef
from compass_backend.artifacts.results import (
    MetricRankingResult,
    RankingRow,
    ResultSet,
)
from compass_backend.contracts.chat import ChatResponse
from compass_backend.contracts.planning import (
    DirectResponse,
    MetricSpec,
    OutputSpec,
    PlannerTurn,
    PolicyGuidancePlan,
    QueryPlan,
    SelectionSpec,
    SortSpec,
    TemporalSpec,
)
from compass_backend.contracts.rendering import ResponseManifest
from compass_backend.contracts.session import SessionState
from compass_backend.quality.verdict_pipeline import _build_context


def _session() -> SessionState:
    now = datetime.now(tz=timezone.utc)
    return SessionState(
        session_id=str(uuid.uuid4()),
        created_at=now,
        updated_at=now,
        turn_count=1,
    )


def _ranking_rows(states: list[str]) -> list[RankingRow]:
    return [
        RankingRow(
            district_id=i + 1,
            district_name=f"District {i + 1}",
            state=state,
            metric_id=100,
            metric_name="enrollment",
            display_value=str(10000 - i * 1000),
            academic_year="2024 - 2025",
            rank=i + 1,
        )
        for i, state in enumerate(states)
    ]


def _result_set(rows: list[RankingRow]) -> ResultSet:
    return MetricRankingResult(
        rows=rows,
        citations=[],
        total_considered=50,
        excluded_count=0,
        order_statement="Sorted by enrollment descending",
    )


def _state_scope_turn(states: list[str]) -> PlannerTurn:
    return PlannerTurn(
        route="execute",
        confidence=0.95,
        query_plan=QueryPlan(
            operation="rank",
            question="show me the top California districts by enrollment",
            selection=SelectionSpec(scope="state", states=states),
            metrics=[MetricSpec(name="enrollment")],
            output=OutputSpec(format="table"),
            temporal=TemporalSpec(intent="current", academic_year="2024 - 2025"),
            sort=SortSpec(field="enrollment", direction="desc"),
        ),
    )


def _named_districts_turn(districts: list[str]) -> PlannerTurn:
    return PlannerTurn(
        route="execute",
        confidence=0.95,
        query_plan=QueryPlan(
            operation="lookup",
            question="compare Houston ISD and Dallas ISD enrollment",
            selection=SelectionSpec(scope="named_districts", districts=districts),
            metrics=[MetricSpec(name="enrollment")],
            output=OutputSpec(format="table"),
            temporal=TemporalSpec(intent="current", academic_year="2024 - 2025"),
        ),
    )


def _direct_turn() -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=0.5,
        direct_response=DirectResponse(
            message="What metric are you interested in?",
            reason="Vague user intent.",
        ),
    )


def _response(
    *,
    turn: PlannerTurn,
    result: ResultSet | None,
    message: str = "Here are the districts.",
    manifest: ResponseManifest | None = None,
) -> ChatResponse:
    session = _session()
    return ChatResponse(
        message=message,
        turn=turn,
        session=session,
        snapshot_id=str(uuid.uuid4()),
        result=result,
        validation=None,
        manifest=manifest,
        trace_id=f"test-trace-{session.session_id[:8]}",
        logfire_url=None,
        message_ids=[],
    )


def _policy_guidance_turn() -> PlannerTurn:
    """Stand-in PlannerTurn for policy_guidance-route tests.

    The pipeline reads `route` and `query_plan` from the turn for snapshot
    building; the PlannerTurn model also requires the route's matching
    payload, so we attach a minimal valid `PolicyGuidancePlan`. The
    context-builder tests below exercise the snapshot path, not the
    policy_guidance planner — the plan contents are not asserted on.
    """
    return PlannerTurn(
        route="policy_guidance",
        confidence=0.85,
        policy_guidance=PolicyGuidancePlan(
            topic_ids=["differentiated-pay"],
            layers=["stances"],
            intent_summary="Topic-only ask resolved to the policy guidance lane.",
        ),
    )


def _policy_guidance_manifest(
    *,
    body: str = "Here is NCTQ policy guidance for Differentiated Pay. [1] [2]",
    citations: list[dict] | None = None,
) -> ResponseManifest:
    return ResponseManifest(
        body=body,
        status="rendered",
        result_type="policy_guidance",
        validation_valid=True,
        warnings=[],
        metadata={"citations": citations or []},
    )


# ── snapshot table.state lock-in ──────────────────────────────────────────────


def test_build_context_table_rows_carry_state_field() -> None:
    """Every row dict in artifact_snapshot.table must carry a `state` key.

    Without this field the selection_set_matches_request validator can't
    detect state-scope mismatches.
    """
    turn = _state_scope_turn(states=["CA"])
    result = _result_set(_ranking_rows(states=["CA", "CA", "CA"]))
    ctx = _build_context(_response(turn=turn, result=result))

    table = ctx.artifact_snapshot.table
    assert len(table) == 3
    for row in table:
        assert row.state == "CA"


def test_build_context_table_rows_carry_sort_display_value() -> None:
    turn = _state_scope_turn(states=["CA"])
    rows = _ranking_rows(states=["CA", "CA"])
    rows[0] = rows[0].model_copy(update={"sort_display_value": "90%"})
    rows[1] = rows[1].model_copy(update={"sort_display_value": "80%"})
    ctx = _build_context(_response(turn=turn, result=_result_set(rows)))

    assert [row.sort_display_value for row in ctx.artifact_snapshot.table] == [
        "90%",
        "80%",
    ]


# ── snapshot selection_expected lock-in ───────────────────────────────────────


def test_build_context_selection_expected_state_scope() -> None:
    """A state-scope plan populates selection_expected.scope='state' + states list."""
    turn = _state_scope_turn(states=["CA", "OR"])
    result = _result_set(_ranking_rows(states=["CA"]))
    ctx = _build_context(_response(turn=turn, result=result))

    sel = ctx.artifact_snapshot.selection_expected
    assert sel is not None, "selection_expected must be present when query_plan exists"
    assert sel.scope == "state"
    assert sorted(sel.states) == ["CA", "OR"]
    assert sel.districts == []


def test_build_context_selection_expected_named_districts_scope() -> None:
    """A named_districts plan populates selection_expected.scope + districts list."""
    turn = _named_districts_turn(districts=["Houston ISD", "Dallas ISD"])
    result = _result_set(_ranking_rows(states=["TX", "TX"]))
    ctx = _build_context(_response(turn=turn, result=result))

    sel = ctx.artifact_snapshot.selection_expected
    assert sel is not None
    assert sel.scope == "named_districts"
    assert sel.states == []
    assert sorted(sel.districts) == ["Dallas ISD", "Houston ISD"]


def test_build_context_selection_expected_absent_on_direct_response() -> None:
    """A direct-response turn (no query_plan) must NOT add selection_expected.

    Validators treat missing selection_expected as "no scope constraint" and
    pass through; this test locks in that behavior at the snapshot layer.
    """
    turn = _direct_turn()
    ctx = _build_context(_response(turn=turn, result=None))

    assert ctx.artifact_snapshot.selection_expected is None


# ── user_message passthrough lock-in ──────────────────────────────────────────


def test_build_context_user_message_threads_through_when_passed() -> None:
    """The raw user prompt must reach ctx.user_message when callers pass it.

    This is what enables `respects_user_limit` (and any future validator
    needing verbatim user text) to read user-stated constraints that the
    planner's derived `intent` drops.
    """
    turn = _direct_turn()
    response = _response(turn=turn, result=None)
    raw_prompt = "top 5 California districts ranked by starting teacher salary"

    ctx = _build_context(response, user_message=raw_prompt)
    assert ctx.user_message == raw_prompt


def test_build_context_user_message_defaults_to_empty_string() -> None:
    """Backward compatibility: callers that don't pass user_message get "".

    Older eval harnesses and any code path that pre-dates the kwarg keep
    working — the validator falls back to ctx.intent in that case.
    """
    turn = _direct_turn()
    response = _response(turn=turn, result=None)

    ctx = _build_context(response)
    assert ctx.user_message == ""


# ── manifest-citation merge lock-in (Cluster B citations fix) ─────────────────
#
# Policy-guidance turns produce no metric ResultSet, so `result.citations` is
# empty even though the renderer emits `[N]` markers in the prose. The renderer
# stores its citation index on `manifest.metadata['citations']`. `_build_context`
# mirrors that index into `snapshot.citations` so every citation validator that
# already reads `artifact_snapshot.citations` sees the markers the body
# references — closing the cross-dimension fail pattern documented in the
# 2026-05-18 fresh sweep cross-sweep analysis.


def test_build_context_policy_guidance_manifest_citations_land_in_snapshot() -> None:
    """Policy_guidance manifest citations populate `snapshot.citations`."""
    turn = _policy_guidance_turn()
    manifest = _policy_guidance_manifest(
        citations=[
            {
                "id": 1,
                "marker": "[1]",
                "stable_id": "stance-diff-pay",
                "title": "NCTQ stance: NBCT Stipends",
                "url": "https://teacherquality.nctq.org/stances/diff-pay",
                "citation_status": "ready",
            },
            {
                "id": 2,
                "marker": "[2]",
                "stable_id": "rationale-diff-pay-1",
                "title": "Dallas ACE program research",
                "url": "https://example.org/dallas-ace",
                "citation_status": "ready",
            },
        ],
    )
    ctx = _build_context(_response(turn=turn, result=None, manifest=manifest))

    markers = sorted(c.marker for c in ctx.artifact_snapshot.citations)
    assert markers == [1, 2]
    titles = {c.title for c in ctx.artifact_snapshot.citations}
    assert "NCTQ stance: NBCT Stipends" in titles


def test_build_context_no_manifest_keeps_snapshot_citations_empty() -> None:
    """Direct-route turns (no manifest) don't gain phantom citations."""
    turn = _direct_turn()
    ctx = _build_context(_response(turn=turn, result=None, manifest=None))

    assert ctx.artifact_snapshot.citations == []


def test_build_context_manifest_citations_dedupe_with_result_citations() -> None:
    """When both sources carry the same marker, `result.citations` wins.

    Result citations are typed-richer (carry academic_year, document_type,
    district_id) than the manifest stub, so we keep them and only append
    manifest entries for markers not already present.
    """
    turn = _state_scope_turn(states=["CA"])
    rows = _ranking_rows(states=["CA"])
    result = MetricRankingResult(
        rows=rows,
        citations=[
            CitationRef(
                marker=1,
                title="Existing result citation",
                url="https://example.org/result",
                academic_year="2024 - 2025",
                document_type="Salary Schedule",
            )
        ],
        total_considered=1,
        excluded_count=0,
        order_statement="Sorted by enrollment descending",
    )
    manifest = _policy_guidance_manifest(
        citations=[
            # Same marker [1] — should be ignored in favor of result.citations.
            {
                "id": 1,
                "marker": "[1]",
                "stable_id": "duplicate-overlap",
                "title": "Manifest duplicate (should be dropped)",
                "citation_status": "ready",
            },
            # Different marker [2] — should be added.
            {
                "id": 2,
                "marker": "[2]",
                "stable_id": "new-marker",
                "title": "Manifest-only citation",
                "citation_status": "ready",
            },
        ],
    )
    ctx = _build_context(_response(turn=turn, result=result, manifest=manifest))

    by_marker = {c.marker: c.title for c in ctx.artifact_snapshot.citations}
    assert by_marker[1] == "Existing result citation"
    assert by_marker[2] == "Manifest-only citation"


def test_build_context_manifest_citations_skips_malformed_entries() -> None:
    """Malformed entries are silently dropped, valid neighbours still flow."""
    turn = _policy_guidance_turn()
    manifest = _policy_guidance_manifest(
        citations=[
            # Valid entry — should land in snapshot.
            {"id": 1, "marker": "[1]", "title": "Good citation"},
            # Bad shape: not a dict.
            "not-a-dict",
            # Missing id.
            {"marker": "[2]", "title": "No id"},
            # id is wrong type.
            {"id": "two", "marker": "[2]", "title": "String id"},
            # Empty title.
            {"id": 3, "marker": "[3]", "title": ""},
            # Negative id.
            {"id": -1, "marker": "[-1]", "title": "Negative id"},
            # Another valid entry — proves the loop kept going.
            {"id": 4, "marker": "[4]", "title": "Also good"},
        ],
    )
    ctx = _build_context(_response(turn=turn, result=None, manifest=manifest))

    by_marker = {c.marker: c.title for c in ctx.artifact_snapshot.citations}
    assert by_marker == {1: "Good citation", 4: "Also good"}


def test_build_context_manifest_citations_field_missing_is_noop() -> None:
    """If `metadata['citations']` is absent, citations stays empty."""
    turn = _policy_guidance_turn()
    # Manifest with empty metadata (no 'citations' key at all).
    manifest = ResponseManifest(
        body="Here is NCTQ policy guidance.",
        status="rendered",
        result_type="policy_guidance",
        validation_valid=True,
        warnings=[],
        metadata={},
    )
    ctx = _build_context(_response(turn=turn, result=None, manifest=manifest))

    assert ctx.artifact_snapshot.citations == []
