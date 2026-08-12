"""Smoke tests for the fresh Compass API workspace."""

import inspect
from unittest.mock import MagicMock

import anyio
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models.test import TestModel

from compass_backend.agents import agent_model_settings
from compass_backend.api.app import create_app as _create_app, create_app_from_settings
import compass_backend.orchestration.chat as chat_module
from compass_backend.orchestration.chat import build_chat_response
from compass_backend.catalog import CandidateCard, MetricCandidate, RecallBatch, RecallReport
from compass_backend.artifacts import (
    CitationRef,
    CoverageFrame,
    MethodologyRef,
    MetricCountResult,
    MetricLookupResult,
    MetricRankingResult,
    MetricValueRow,
    PeerComparisonResult,
    PeerComparisonRow,
    RankingRow,
    ResultSelection,
    ResultSet,
    SelectedDistrict,
    ThresholdCountRow,
)
from compass_backend.contracts import (
    ChatRequest,
    ClarificationOption,
    ClarificationRequest,
    ConversationMemory,
    DirectResponse,
    FilterSpec,
    LimitSpec,
    MetricSpec,
    OutputSpec,
    PendingQueryContext,
    PolicyGuidancePlan,
    PlannerTurn,
    PlannerRunEvidence,
    ProfileFieldSpec,
    QueryContext,
    QueryPlan,
    ConversationSummary,
    ResultMemoryRef,
    ResponseManifest,
    SelectionSpec,
    SortSpec,
    SortStepSpec,
)
from compass_backend.config import Settings
from compass_backend.contracts.validation import ValidationAuthority
from compass_backend.contracts.recognition import (
    PlanningRecognitionMention,
)
from compass_backend.execution import (
    ExecutionClarification,
    ExecutionOutcome,
    ExecutionRefusal,
    ExecutionSuccess,
)
import compass_backend.planning.planner as planner_module
from compass_backend.planning import (
    PlannerDeps,
    PlannerRun,
    planner_context_instructions,
    planner_guidance_authority_warning,
    planner_guidance_instructions,
    planner_runtime_context_instructions,
    run_planner,
    validate_planner_turn_quality,
)
from compass_backend.planning.instruction_snippets import (
    select_planner_instruction_snippets,
)
from compass_backend.policy_guidance import (
    ExemplarPolicy,
    PolicyGuidanceLibrary,
    Stance,
    TopicGuidance,
)
from compass_backend.policy_guidance.library import (
    load_default_library,
    reset_library,
    set_library,
)
from compass_backend.rendering.policy_guidance import (
    POLICY_GUIDANCE_VALIDATION_FAILED_BODY,
)
from compass_backend.session import InMemorySessionStore


@pytest.fixture(autouse=True)
def _suppress_verdict_pipeline_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the async verdict pipeline from opening a real Postgres pool.

    These route-level tests use injected stores/executors and no DB. The chat
    route schedules ``verdict_pipeline.fire_and_forget`` as a background task,
    which calls ``asyncpg.create_pool`` against the local ``PG_*`` default and
    surfaces as a cross-test connection error in the full suite (each passes in
    isolation). No-op it for every test in this module; a test that needs its
    own spy still overrides this with its own ``monkeypatch.setattr``.
    """

    monkeypatch.setattr(
        "compass_backend.api.chat.verdict_pipeline.fire_and_forget",
        MagicMock(),
    )


def _unit_chat_settings(app_settings: Settings | None = None) -> Settings:
    """Keep route-level unit tests isolated from live catalog dependencies."""

    base = app_settings or Settings(session_store_backend="memory")
    return base.model_copy(
        update={
            "catalog_recall_shadow_enabled": False,
            "catalog_resolver_recall_enabled": False,
            # Keep route-level unit tests off the live answer-layer stylist.
            # Default "gated" mode invokes the real Opus stylist via the
            # gateway, whose non-deterministic output intermittently replaces
            # the deterministic renderer body these tests assert on (#1089).
            # The answer layer has its own dedicated tests with a stubbed
            # stylist; here we preserve deterministic renderer output.
            "answer_layer_mode": "off",
        }
    )


def create_app(*args, app_settings: Settings | None = None, **kwargs):
    return _create_app(
        *args,
        app_settings=_unit_chat_settings(app_settings),
        **kwargs,
    )


class FakeQueryExecutor:
    """Fake deterministic executor for chat route tests."""

    def __init__(self, outcome: ExecutionOutcome) -> None:
        self.outcome = outcome
        self.plans: list[QueryPlan] = []

    async def execute(self, plan: QueryPlan) -> ExecutionOutcome:
        self.plans.append(plan)
        return self.outcome


class FakeCatalogRecallService:
    """Fake advisory recall service for orchestration shadow-mode tests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def recall(
        self,
        query: str,
        *,
        entity_types=None,
        limit: int = 10,
        states: set[str] | None = None,
        expand_prompt: bool = True,
    ) -> RecallReport:
        requested = tuple(entity_types or ("metric",))
        self.calls.append(
            {
                "query": query,
                "entity_types": requested,
                "limit": limit,
                "states": states,
                "expand_prompt": expand_prompt,
            }
        )
        return RecallReport(
            query=query,
            batches=[
                RecallBatch(
                    query=query,
                    entity_types=list(requested),
                    candidates=[
                        CandidateCard(
                            input_phrase=query,
                            entity_type="metric",
                            label=f"Official candidate for {query}",
                            plain_definition="Synthetic recall candidate.",
                            source_methods=["metric_search"],
                            debug_ref=f"metric:{len(self.calls)}",
                        )
                    ],
                    limit=limit,
                )
            ],
        )


class SequencePlannerAgent:
    """Fake planner that returns a configured sequence of planner turns."""

    def __init__(self, turns: list[PlannerTurn]) -> None:
        self.turns = list(turns)
        self.prompts: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    async def run(self, user_prompt: str, **kwargs):
        from pydantic_ai.agent import AgentRunResult

        self.prompts.append(user_prompt)
        self.kwargs.append(kwargs)
        turn = self.turns.pop(0)
        return AgentRunResult(output=turn)


class StructuredFailurePlannerAgent:
    """Fake planner that simulates Pydantic AI structured-output exhaustion."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    async def run(self, user_prompt: str, **kwargs):
        self.prompts.append(user_prompt)
        self.kwargs.append(kwargs)
        raise UnexpectedModelBehavior(
            "Exceeded maximum retries (0) for output validation"
        )


class SequenceQueryExecutor:
    """Fake deterministic executor that records merged query plans."""

    def __init__(self, outcomes: list[ExecutionOutcome]) -> None:
        self.outcomes = list(outcomes)
        self.plans: list[QueryPlan] = []

    async def execute(self, plan: QueryPlan) -> ExecutionOutcome:
        self.plans.append(plan)
        return self.outcomes.pop(0)


def _agent_for_turn(turn: PlannerTurn) -> Agent:
    # call_tools=[] keeps the canned-output TestModel from auto-invoking the
    # always-attached Compass catalog toolset (#1248). These offline route
    # tests assert deterministic planner output, not catalog-tool behavior;
    # the unpopulated test ChatPoolHolder has no DB for a tool call to hit.
    return Agent(
        TestModel(
            custom_output_args=turn.model_dump(mode="json"),
            call_tools=[],
        ),
        output_type=PlannerTurn,
    )


def test_run_planner_passes_raw_prompt_and_context_as_typed_deps() -> None:
    planner = SequencePlannerAgent([_direct_turn("Planned.")])
    previous_plan = QueryPlan(
        question="Compare starting salaries.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Chicago Public Schools", "Denver Public Schools"],
        ),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format="table"),
    )
    query_context = QueryContext(
        query_plan=previous_plan,
        result_type="metric_lookup",
        result_districts=[
            {
                "district_id": 1,
                "district_name": "Chicago Public Schools",
                "state": "IL",
            }
        ],
    )
    pending_context = PendingQueryContext(
        operation="lookup",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="teacher salary")],
        missing_fields=["metric"],
    )

    async def run_it():
        return await run_planner(
            "Sort those highest first.",
            model="test-model",
            agent=planner,
            context=query_context,
            pending_context=pending_context,
            recent_routes=["execute"],
            trace_id="trace-123",
        )

    run = anyio.run(run_it)

    assert run.turn.direct_response is not None
    assert planner.prompts == ["Sort those highest first."]
    deps = planner.kwargs[0]["deps"]
    assert deps.message == "Sort those highest first."
    assert deps.query_context == query_context
    assert deps.pending_context == pending_context
    assert deps.recent_routes == ("execute",)
    assert deps.trace_id == "trace-123"


def test_run_planner_attaches_per_run_toolsets_only_when_supplied() -> None:
    planner = SequencePlannerAgent([_direct_turn("Planned.")])
    toolset = object()

    async def run_it():
        return await run_planner(
            "Finalize with tools.",
            model="test-model",
            agent=planner,
            planner_toolsets=[toolset],  # type: ignore[list-item]
        )

    run = anyio.run(run_it)

    assert run.turn.direct_response is not None
    assert planner.kwargs[0]["toolsets"] == [toolset]


def test_run_planner_always_bounds_tool_calls_with_usage_limits() -> None:
    """#1248 PR2a: the single clean planner path attaches the catalog tool on
    every run, so ``run_planner`` must always pass the module-level
    ``PLANNER_USAGE_LIMITS`` so a runaway tool loop is bounded."""

    from compass_backend.planning.planner import PLANNER_USAGE_LIMITS

    planner = SequencePlannerAgent([_direct_turn("Planned.")])

    async def run_it():
        return await run_planner(
            "Plan this.",
            model="test-model",
            agent=planner,
        )

    anyio.run(run_it)

    assert planner.kwargs[0]["usage_limits"] is PLANNER_USAGE_LIMITS


def test_primary_planner_pass_receives_catalog_toolset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1248 PR2a: the catalog toolset is always attached to the PRIMARY planner
    pass (so the planner can look things up before it drafts), not only to a
    finalization pass."""

    toolset = object()
    pipeline = object()
    calls: list[dict[str, object]] = []

    async def fake_run_planner(*_args, **kwargs) -> PlannerRun:
        calls.append(kwargs)
        return PlannerRun(
            turn=_direct_turn("Planned."), evidence=PlannerRunEvidence()
        )

    monkeypatch.setattr(chat_module, "run_planner", fake_run_planner)

    async def run_response():
        return await build_chat_response(
            ChatRequest(message="rank districts by starting salary"),
            planner_agent=_agent_for_turn(_direct_turn("unused")),
            store=InMemorySessionStore(),
            executor=FakeQueryExecutor(ExecutionRefusal(message="unused")),
            app_settings=Settings(session_store_backend="memory"),
            planner_toolsets=[toolset],  # type: ignore[list-item]
            catalog_pipeline=pipeline,  # type: ignore[arg-type]
        )

    anyio.run(run_response)

    # One pass only — no pass B. Both the catalog tool and the always-on
    # verifier (catalog_pipeline) are threaded to the PRIMARY pass, so the
    # planner can look things up before drafting AND the verifier's
    # ModelRetry-with-candidates custody check fires on the single path.
    assert len(calls) == 1
    assert calls[0]["planner_toolsets"] == [toolset]
    assert calls[0]["catalog_pipeline"] is pipeline


def test_freed_planner_execute_over_hard_blocker_is_caught_on_single_path() -> None:
    """#1248 PR2b — W1-01 custody invariant on the ONE clean planner path.

    The pass-B recognition pass used to force a blocker turn when a finalize
    rerun returned an executable plan while recognition still had a hard
    blocker. With pass B deleted, the custody "end" must survive on the single
    path through the always-on verifier: when the planner drafts ``execute``
    over a hard catalog blocker, the output validator
    (``validate_planner_turn_quality_async`` → ``catalog_pipeline``) raises
    ``ModelRetry`` with real candidates; with ``retries={"output": 0}`` that
    surfaces as ``UnexpectedModelBehavior`` and orchestration converts it to a
    rescue clarification. The unverified execute plan must NEVER reach the
    executor.

    This passes with pass B present and after pass B is deleted — it asserts
    the single-path behavior, not the pass-B topology. If a future change frees
    the planner to execute over a hard blocker (e.g. the verifier is detached
    from the primary pass), this test fails.
    """

    from compass_backend.contracts.planning import OutputSpec
    from compass_backend.planning.catalog_pipeline import CatalogPlanPipeline
    from compass_backend.catalog.reconciliation import CatalogReconciler
    from compass_backend.planning.planner import create_planner_agent

    # An unsupported-concept catalog: "union release time" has no governed
    # metric, so reconcile→finalize emits a hard UnsupportedConceptBlocker.
    class _UnsupportedConceptCatalog:
        async def resolve_unsupported_concepts(self, phrases):
            from compass_backend.catalog import CatalogResolutionEntity

            results = []
            for phrase in phrases:
                if phrase.casefold().strip() == "union release time":
                    results.append(
                        CatalogResolutionEntity(
                            input_phrase=phrase,
                            entity_type="unsupported_concept",
                            status="unsupported",
                            resolution_method="unsupported_catalog",
                            approved_key="union_release_time",
                            label="Union release time",
                            provenance="test",
                            message="No governed metric for union release time.",
                        )
                    )
            return results

        async def resolve_metric_bundle(self, query, **_kwargs):
            from compass_backend.catalog import MetricBundleResolution

            return MetricBundleResolution(query=query)

        async def resolve_profile_field_authority(self, query, **_kwargs):
            from compass_backend.catalog import ProfileFieldResolution

            return ProfileFieldResolution(query=query)

    pipeline = CatalogPlanPipeline(
        CatalogReconciler(_UnsupportedConceptCatalog()), adjudicator=None
    )

    # The planner DRAFTS an execute plan over the hard blocker. We use the real
    # production planner agent (its output validator runs the verifier) with a
    # TestModel feeding the unverified draft.
    execute_over_blocker = PlannerTurn(
        route="execute",
        confidence=0.5,
        query_plan=QueryPlan(
            operation="lookup",
            question="What about union release time in Philadelphia?",
            selection=SelectionSpec(
                scope="named_districts", districts=["Philadelphia"]
            ),
            metrics=[MetricSpec(name="union release time", role="primary")],
            output=OutputSpec(format="text"),
        ),
    )
    test_model = TestModel(
        custom_output_args=execute_over_blocker.model_dump(mode="json"),
        call_tools=[],
    )
    planner_agent = create_planner_agent(test_model)  # type: ignore[arg-type]

    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_ranking_result(),
            authority=ValidationAuthority(),
            message="should never be reached",
        )
    )

    async def run_response():
        return await build_chat_response(
            ChatRequest(message="What about union release time in Philadelphia?"),
            planner_agent=planner_agent,
            store=InMemorySessionStore(),
            executor=executor,
            app_settings=Settings(session_store_backend="memory"),
            catalog_pipeline=pipeline,
        )

    response = anyio.run(run_response)

    # The freed execute plan was caught: orchestration pivoted to a clarify
    # turn instead of executing on the unverified plan. The clarification is the
    # governed rescue template (the verifier's ModelRetry surfaced and was
    # converted via _planner_structured_output_failure_turn, then enriched by
    # _enrich_rescue_with_prior_context — which rebuilds the request and is the
    # reason is_rescue_fallback is no longer flagged here). The template prefix
    # proves it is the verifier-driven rescue, not a model-authored clarify.
    assert response.turn.route == "clarify"
    assert response.turn.clarification is not None
    assert response.turn.clarification.question.startswith(
        "I need one district group and one Compass metric to start."
    )
    # The unverified execute plan NEVER reached the executor.
    assert executor.plans == []


def test_planner_context_instructions_render_context_from_typed_deps() -> None:
    previous_plan = QueryPlan(
        question="Compare starting salaries.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format="table"),
    )
    deps = PlannerDeps(
        message="Sort those highest first.",
        current_academic_year="2024 - 2025",
        memory=ConversationMemory(
            pending_query_context=PendingQueryContext(
                operation="lookup",
                selection=SelectionSpec(scope="state", states=["CA"]),
                metrics=[MetricSpec(name="teacher salary")],
                missing_fields=["metric"],
            ),
            latest_query_context=QueryContext(
                query_plan=previous_plan,
                result_type="metric_lookup",
                order_statement="Looked up starting salary.",
                row_count=1,
                result_districts=[
                    {
                        "district_id": 2,
                        "district_name": "Bravo",
                        "state": "CA",
                    }
                ],
            ),
            recent_routes=["execute"],
        ),
    )

    instructions = planner_context_instructions(deps)

    assert "Non-authoritative Compass conversation memory" in instructions
    assert "pending_query" in instructions
    # W0.5 (#832): the recent_transcript prose block was deleted in favor of
    # message_history threaded into planner.run(); other memory blocks
    # (pending_query, prior_results, summary) still render.
    assert "recent_transcript" not in instructions
    assert "2024 - 2025" in instructions
    assert "starting salary" in instructions
    assert "Bravo" in instructions
    assert "Current user message" not in instructions
    assert "Sort those highest first." not in instructions


def test_planner_dynamic_instructions_split_runtime_context_from_guidance() -> None:
    previous_plan = QueryPlan(
        question="Compare starting salaries.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
    )
    deps = PlannerDeps(
        message="Sort those highest first.",
        memory=ConversationMemory(
            latest_query_context=QueryContext(query_plan=previous_plan),
        ),
    )
    deps = deps.with_guidance(select_planner_instruction_snippets(deps))

    runtime = planner_runtime_context_instructions(deps)
    guidance = planner_guidance_instructions(deps)
    warning = planner_guidance_authority_warning(deps)

    assert "Non-authoritative Compass conversation memory" in runtime
    assert "planner_guidance" not in runtime
    assert "ranking-and-sorting" in guidance
    assert "follow-up-reference" in guidance
    assert "Sort those highest first." not in guidance
    assert "Planner guidance is planning guidance only" in warning
    assert "not execution authority" in warning


def test_planner_context_instructions_render_conversation_memory_and_result_refs() -> None:
    summary = ConversationSummary(
        summary="User is exploring salary comparisons and rejected benefits.",
        active_user_goal="Find a salary comparison worth charting.",
        open_questions=["Which salary lane should Compass use?"],
        accepted_choices=["California districts"],
        rejected_choices=["Benefits metrics"],
        user_preferences=["Prefers charts over prose summaries"],
    )
    result_ref = ResultMemoryRef(
        snapshot_id="snapshot-2",
        turn_index=2,
        question="Rank California salaries.",
        result_type="metric_ranking",
        row_count=133,
        displayed_row_count=10,
        display_limit=10,
        metrics=[{"metric_id": 89, "metric_name": "Starting salary"}],
        districts=[{"district_id": 1, "district_name": "Alpha USD", "state": "CA"}],
        has_chart=True,
        has_csv_export=True,
        digest="133 California salary rows; 10 shown in the preview.",
    )
    deps = PlannerDeps(
        message="Let's try that one as a chart.",
        memory=ConversationMemory(
            summary=summary,
            result_refs=[result_ref],
            latest_turn_index=2,
            source_snapshot_ids=["snapshot-1", "snapshot-2"],
        ),
    )

    instructions = planner_context_instructions(deps)

    assert "Non-authoritative Compass conversation memory" in instructions
    assert "exploring salary comparisons" in instructions
    assert "Benefits metrics" in instructions
    assert "prior_results" in instructions
    assert "snapshot-2" in instructions
    assert "summary text is interpretive context only" in instructions


def test_planner_guidance_selects_ranking_snippet_only_for_ranking_prompt() -> None:
    deps = PlannerDeps(message="Show the five highest enrollment districts.")

    selected = select_planner_instruction_snippets(deps)

    assert [item.name for item in selected] == ["ranking-and-sorting"]
    assert selected[0].matched_phrase == "highest"


def test_planner_guidance_selects_followup_snippet_when_prior_context_exists() -> None:
    previous_plan = QueryPlan(
        question="Compare starting salaries.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
    )
    deps = PlannerDeps(
        message="Sort those highest first.",
        memory=ConversationMemory(
            latest_query_context=QueryContext(query_plan=previous_plan),
        ),
    )

    selected = select_planner_instruction_snippets(deps)

    assert [item.name for item in selected] == [
        "follow-up-reference",
        "ranking-and-sorting",
    ]


def test_planner_guidance_selects_salary_topic_snippet() -> None:
    deps = PlannerDeps(message="How much do teachers make in Dallas?")

    selected = select_planner_instruction_snippets(deps)

    assert [item.name for item in selected] == ["teacher-compensation-salary"]
    assert selected[0].metadata == {
        "topic_name": "Teacher Compensation",
        "subtopic_name": "Salary",
    }


def test_salary_guidance_documents_ba_ma_composite_rank_shape() -> None:
    deps = PlannerDeps(
        message="starting salary for teachers with a BA and teachers with an MA"
    )

    selected = select_planner_instruction_snippets(deps)
    salary = next(
        item for item in selected if item.name == "teacher-compensation-salary"
    )

    assert "requires_composite_ranking=True" in salary.body
    assert "degree_lane=\"ba\"" in salary.body
    assert "degree_lane=\"ma\"" in salary.body
    assert "unsupported-shape" in salary.body


def test_planner_guidance_selects_coverage_state_language_snippet() -> None:
    deps = PlannerDeps(
        message="Does Compass have current data for Austin ISD, or is it older data?"
    )

    selected = select_planner_instruction_snippets(deps)

    assert [item.name for item in selected] == ["coverage-state-language"]
    assert selected[0].matched_phrase == "older data"
    assert "skip refusal" in selected[0].body
    assert "execution and rendering own the exact coverage explanation" in selected[0].body


def test_planner_guidance_selects_policy_guidance_followup_snippet() -> None:
    deps = PlannerDeps(
        message="Now show me the actual policy details for the top one.",
        memory=ConversationMemory(recent_routes=["policy_guidance"]),
    )

    selected = select_planner_instruction_snippets(deps)

    assert [item.name for item in selected] == ["policy-guidance-followups"]
    assert selected[0].matched_phrase == "details"
    assert "Retain the policy-guidance referent" in selected[0].body


def test_planner_guidance_selects_profile_sort_salary_display_snippet() -> None:
    # Near-equivalent of the reporter's literal prompt (B-spine case
    # REGR-M1-CLOSURE-FRPL-SALARY-RANK, case_id 1035); kept distinct from the
    # snippet's own canonical example so the no-echo assertion below stays
    # meaningful.
    deps = PlannerDeps(
        message=(
            "Show me the starting teacher salaries in districts that have the "
            "highest free-and-reduced lunch share."
        )
    )

    selected = select_planner_instruction_snippets(deps)

    assert [item.name for item in selected] == [
        "ranking-and-sorting",
        "teacher-compensation-salary",
        "profile-sort-salary-display",
    ]
    payload = selected[-1].model_payload()
    assert "profile-sort-salary-display" == payload["name"]
    assert deps.message not in str(payload)
    assert "profile-field selection context" in selected[-1].body


def test_planner_guidance_selects_profile_sort_salary_display_for_lowest_frpl() -> None:
    deps = PlannerDeps(
        message=(
            "Show me starting teacher salaries for teachers with a masters for "
            "districts with the lowest free-and-reduced lunch share."
        )
    )

    selected = select_planner_instruction_snippets(deps)

    assert [item.name for item in selected] == [
        "ranking-and-sorting",
        "teacher-compensation-salary",
        "profile-sort-salary-display",
    ]


def test_planner_guidance_selects_profile_sort_metric_display_for_enrollment() -> None:
    # REGR-M1-CLOSURE-MA-ENROLLMENT-SORT-C00: "show <policy metric> for the
    # districts with the highest <profile field>" must rank/select BY the
    # profile field and DISPLAY the policy metric. The new snippet must land in
    # the selected top-3 (max_snippets=3) alongside the general ranking and
    # salary snippets.
    deps = PlannerDeps(
        message=(
            "Show me master's starting salaries for the districts with the "
            "highest enrollment."
        )
    )

    selected = select_planner_instruction_snippets(deps)

    names = [item.name for item in selected]
    assert "profile-sort-metric-display" in names
    assert len(selected) <= 3
    snippet = next(item for item in selected if item.name == "profile-sort-metric-display")
    assert snippet.metadata == {"intent": "profile_ordered_metric_display"}


def test_profile_sort_metric_display_absent_for_plain_enrollment_ranking() -> None:
    # A bare "rank districts by enrollment" carries no policy-metric phrase, so
    # the metric-display snippet must NOT fire — RANKING_AND_SORTING owns it.
    deps = PlannerDeps(message="Rank all districts by enrollment.")

    selected = select_planner_instruction_snippets(deps)

    names = [item.name for item in selected]
    assert "profile-sort-metric-display" not in names
    assert "ranking-and-sorting" in names


def test_profile_sort_salary_display_unchanged_by_metric_display_snippet() -> None:
    # The FRPL prompt selection must be unchanged: the new metric-display
    # snippet must not displace profile-sort-salary-display under the 3-cap.
    deps = PlannerDeps(
        message=(
            "Show me starting teacher salaries for districts with the highest "
            "free-and-reduced lunch share."
        )
    )

    selected = select_planner_instruction_snippets(deps)

    names = [item.name for item in selected]
    assert "profile-sort-salary-display" in names
    assert "profile-sort-metric-display" not in names


def test_profile_sort_metric_display_absent_without_policy_metric() -> None:
    # "5 largest districts by enrollment" has no policy metric to display, so the
    # metric-display snippet must NOT fire.
    deps = PlannerDeps(message="the 5 largest districts by enrollment")

    selected = select_planner_instruction_snippets(deps)

    names = [item.name for item in selected]
    assert "profile-sort-metric-display" not in names


def test_profile_sort_metric_display_absent_when_largest_modifies_the_metric() -> None:
    # #1315: "largest" here modifies the SALARY, not a district set — there is no
    # profile field, so the profile-ordered snippet must NOT fire. The trigger is
    # scoped to "largest district(s)", so "largest starting salaries" stays clear.
    deps = PlannerDeps(message="Show me the largest starting salaries")

    names = [item.name for item in select_planner_instruction_snippets(deps)]

    assert "profile-sort-metric-display" not in names


def test_profile_sort_metric_display_fires_for_largest_districts_phrasing() -> None:
    # The district-size phrasing ("for the largest districts") still reaches the
    # profile-ordered recipe when a policy metric is present to display.
    deps = PlannerDeps(message="Show me teacher salaries for the largest districts")

    names = [item.name for item in select_planner_instruction_snippets(deps)]

    assert "profile-sort-metric-display" in names


def test_profile_sort_metric_display_body_documents_selection_phase_shape() -> None:
    deps = PlannerDeps(
        message=(
            "Show me master's starting salaries for the districts with the "
            "highest enrollment."
        )
    )

    selected = select_planner_instruction_snippets(deps)
    snippet = next(item for item in selected if item.name == "profile-sort-metric-display")

    assert "selection-phase" in snippet.body
    assert "profile_field" in snippet.body
    assert "KEEP the user's policy metric" in snippet.body


def test_planner_guidance_selects_peer_salary_comparison_snippet() -> None:
    deps = PlannerDeps(
        message=(
            "What is the maximum teacher salary in San Bernardino City Unified "
            "and comparable districts?"
        )
    )

    selected = select_planner_instruction_snippets(deps)

    # With PR 2B, similarity-discovery (priority 38) also fires on "comparable"
    # before peer-salary-comparison (priority 40).  Both snippets fire; the
    # disambiguation rule in similarity-discovery instructs the LLM to prefer
    # peer_comparison when a metric is mentioned.
    snippet_names = [item.name for item in selected]
    assert "teacher-compensation-salary" in snippet_names
    assert "peer-salary-comparison" in snippet_names
    assert "similarity-discovery" in snippet_names
    peer_salary_snippet = next(s for s in selected if s.name == "peer-salary-comparison")
    assert peer_salary_snippet.matched_phrase == "comparable"
    assert peer_salary_snippet.metadata["required_operation"] == "peer_comparison"
    assert "operation=\"peer_comparison\"" in peer_salary_snippet.body


def test_planner_guidance_selects_peer_policy_comparison_snippet() -> None:
    deps = PlannerDeps(
        message=(
            "I work in Denver Public Schools. Who are our peer districts and "
            "how do our sick leave policies compare?"
        )
    )

    selected = select_planner_instruction_snippets(deps)

    snippet_names = [item.name for item in selected]
    assert "peer-policy-comparison" in snippet_names
    peer_policy_snippet = next(
        s for s in selected if s.name == "peer-policy-comparison"
    )
    assert peer_policy_snippet.metadata["required_operation"] == "peer_comparison"
    assert "operation=\"peer_comparison\"" in peer_policy_snippet.body
    assert "sick leave policy" in peer_policy_snippet.body
    assert "selection.districts" in peer_policy_snippet.body


def test_planner_guidance_selects_similarity_discovery_for_peer_only_prompt() -> None:
    """'Find peers to Denver' (no metric) → similarity-discovery snippet fires."""

    deps = PlannerDeps(message="Find peers to Denver.")

    selected = select_planner_instruction_snippets(deps)

    snippet_names = [item.name for item in selected]
    assert "similarity-discovery" in snippet_names
    similarity_snippet = next(s for s in selected if s.name == "similarity-discovery")
    assert "operation=\"similarity\"" in similarity_snippet.body


def test_planner_guidance_selects_similarity_discovery_for_comparable_enrollment() -> None:
    """'Comparable enrollment outside California' → similarity-discovery snippet fires."""

    deps = PlannerDeps(
        message="Find comparable enrollment districts outside California for Denver."
    )

    selected = select_planner_instruction_snippets(deps)

    snippet_names = [item.name for item in selected]
    assert "similarity-discovery" in snippet_names


def test_planner_guidance_selects_similarity_discovery_for_frpl_peers() -> None:
    """'Find peers to Denver by FRPL share' → similarity-discovery fires."""

    deps = PlannerDeps(message="Find peers to Denver by FRPL share.")

    selected = select_planner_instruction_snippets(deps)

    snippet_names = [item.name for item in selected]
    assert "similarity-discovery" in snippet_names
    # peer-salary-comparison should NOT fire (no salary + maximum phrase group)
    assert "peer-salary-comparison" not in snippet_names


def test_planner_guidance_peer_salary_wins_when_salary_and_max_present() -> None:
    """'Find peers to Denver and compare maximum salary' → peer-salary-comparison fires."""

    deps = PlannerDeps(
        message=(
            "Find me 10 peers to Denver, then compare maximum teacher salary."
        )
    )

    selected = select_planner_instruction_snippets(deps)

    snippet_names = [item.name for item in selected]
    # Both similarity-discovery (priority 38) and peer-salary-comparison (priority 40)
    # fire — the disambiguation rule in similarity-discovery teaches the LLM to prefer
    # peer_comparison when a metric is mentioned.
    assert "similarity-discovery" in snippet_names
    assert "peer-salary-comparison" in snippet_names


def test_planner_guidance_ignores_unrelated_and_policy_guidance_prompts() -> None:
    unrelated = PlannerDeps(message="Hello there.")
    policy_guidance = PlannerDeps(
        message=(
            "We want to redesign our salary schedule. Can you show me a "
            "district that does this well?"
        )
    )

    assert select_planner_instruction_snippets(unrelated) == ()
    assert select_planner_instruction_snippets(policy_guidance) == ()


def test_planner_guidance_does_not_select_profile_salary_snippet_for_plain_salary() -> None:
    deps = PlannerDeps(message="Show the five highest starting salaries.")

    selected = select_planner_instruction_snippets(deps)

    assert [item.name for item in selected] == [
        "ranking-and-sorting",
        "teacher-compensation-salary",
    ]


def test_planner_guidance_enforces_max_snippet_cap() -> None:
    previous_plan = QueryPlan(
        question="Compare starting salaries.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
    )
    deps = PlannerDeps(
        message="Sort those highest first by salary.",
        memory=ConversationMemory(
            latest_query_context=QueryContext(query_plan=previous_plan),
        ),
    )

    selected = select_planner_instruction_snippets(deps, max_snippets=2)

    assert [item.name for item in selected] == [
        "follow-up-reference",
        "ranking-and-sorting",
    ]


def test_planner_context_instructions_render_selected_guidance_without_user_message() -> None:
    deps = PlannerDeps(message="Show the five highest starting salaries.")
    deps = deps.with_guidance(select_planner_instruction_snippets(deps))

    instructions = planner_context_instructions(deps)

    assert "Compass planner guidance snippets" in instructions
    assert "planning guidance only" in instructions
    assert "Show the five highest starting salaries." not in instructions


def test_planner_guidance_instructions_render_markdown_snippet_body() -> None:
    deps = PlannerDeps(message="Rank all covered districts by starting salary.")
    deps = deps.with_guidance(select_planner_instruction_snippets(deps))

    instructions = planner_guidance_instructions(deps)

    assert "Ranking and sorting" in instructions
    assert "Teacher compensation salary" in instructions
    assert "sort_steps" in instructions


def test_planner_constitution_keeps_broad_rules_while_ranking_snippet_holds_microadvice() -> None:
    from compass_backend.planning.planner import PLANNER_INSTRUCTIONS

    deps = PlannerDeps(message="Rank the 10 largest districts by starting salary.")
    deps = deps.with_guidance(select_planner_instruction_snippets(deps))
    guidance = planner_guidance_instructions(deps)

    assert "count_kind" in PLANNER_INSTRUCTIONS
    assert 'operation="count"' in PLANNER_INSTRUCTIONS
    assert "profile_lookup" in PLANNER_INSTRUCTIONS
    assert 'operation="rank"' in PLANNER_INSTRUCTIONS
    assert "Use ordered `sort_steps`" in PLANNER_INSTRUCTIONS

    assert "10 largest districts by enrollment" not in PLANNER_INSTRUCTIONS
    assert "free-and-reduced lunch share" not in PLANNER_INSTRUCTIONS
    assert "merely because" not in PLANNER_INSTRUCTIONS
    assert "profile-sort and salary-display" not in PLANNER_INSTRUCTIONS
    assert "unbounded" not in PLANNER_INSTRUCTIONS

    assert "sort_steps" in PLANNER_INSTRUCTIONS
    assert "reviewed BA default" in PLANNER_INSTRUCTIONS
    assert "operation=\"lookup\"" in PLANNER_INSTRUCTIONS
    assert "row_display=\"all\"" in PLANNER_INSTRUCTIONS

    assert "Ranking and sorting" in guidance
    assert "10 largest districts by enrollment" in guidance
    assert "free-and-reduced lunch share" in guidance
    assert "merely because" in guidance
    assert "profile-sort and salary-display" in guidance
    assert "unbounded" in guidance


def test_planner_instructions_make_strike_state_followup_executable() -> None:
    from compass_backend.planning.planner import PLANNER_INSTRUCTIONS

    assert "Which states allow strikes?" in PLANNER_INSTRUCTIONS
    assert "Legality of teacher strikes" in PLANNER_INSTRUCTIONS
    assert 'output.group_by="state"' in PLANNER_INSTRUCTIONS
    assert "NCTQ's position on strikes is `policy_guidance`" in PLANNER_INSTRUCTIONS


def test_planner_instructions_rank_health_premium_percentage_metric() -> None:
    from compass_backend.planning.planner import PLANNER_INSTRUCTIONS

    assert "Which districts cover the most of teachers' health insurance premiums?" in (
        PLANNER_INSTRUCTIONS
    )
    assert 'MetricSpec(name="health insurance premiums")' in PLANNER_INSTRUCTIONS
    assert 'SortSpec(field="health insurance premiums", direction="desc")' in (
        PLANNER_INSTRUCTIONS
    )
    assert 'value="Yes"' in PLANNER_INSTRUCTIONS
    assert 'do not use `value="100%"`' in PLANNER_INSTRUCTIONS


def test_planner_no_longer_has_retry_helper_token_checks() -> None:
    source = inspect.getsource(planner_module)

    forbidden_names = [
        "_is_frpl_starting_salary_display_clarification",
        "_is_peer_max_teacher_salary_clarification",
        "_is_policy_guidance_detail_followup",
        "_POLICY_GUIDANCE_DETAIL_TOKENS",
        "_STARTING_TOKENS",
        "_FRPL_TOKENS",
        "_MAXIMUM_TOKENS",
        "_PEER_TOKENS",
    ]
    for name in forbidden_names:
        assert name not in source


def _direct_turn(message: str = "Hello. I can help plan Compass questions.") -> PlannerTurn:
    return PlannerTurn(
        route="direct",
        confidence=0.98,
        direct_response=DirectResponse(
            message=message,
            reason="Greeting does not require data execution.",
        ),
    )


def _toy_policy_guidance_library() -> PolicyGuidanceLibrary:
    stance = Stance(
        stance_id="stance:runtime-topic-x",
        topic_id="runtime-topic",
        title="Runtime Topic",
        body="Runtime topic stance.",
    )
    second_stance = Stance(
        stance_id="stance:second-runtime-topic-x",
        topic_id="second-runtime-topic",
        title="Second Runtime Topic",
        body="Second runtime topic stance.",
    )
    topic = TopicGuidance(
        topic_id="runtime-topic",
        canonical_topic="Runtime Topic",
        aliases=("runtime topic",),
        canonical_url=None,
        topic_brief="Runtime topic brief.",
        stances=(stance,),
        rationales=(),
        exemplars=(),
    )
    second_topic = TopicGuidance(
        topic_id="second-runtime-topic",
        canonical_topic="Second Runtime Topic",
        aliases=("second runtime topic",),
        canonical_url=None,
        topic_brief="Second runtime topic brief.",
        stances=(second_stance,),
        rationales=(),
        exemplars=(),
    )
    return PolicyGuidanceLibrary.build(
        topics={
            "runtime-topic": topic,
            "second-runtime-topic": second_topic,
        }
    )


def _performance_pay_policy_guidance_library() -> PolicyGuidanceLibrary:
    topic = TopicGuidance(
        topic_id="differentiated-pay",
        canonical_topic="Differentiated Pay",
        aliases=("differentiated pay", "performance pay"),
        canonical_url=None,
        topic_brief="Differentiated pay brief.",
        stances=(),
        rationales=(),
        exemplars=(
            ExemplarPolicy(
                exemplar_id="exemplar:dcps-performance-pay",
                topic_id="differentiated-pay",
                district="District of Columbia Public Schools",
                district_id=150,
                subtopic="Performance pay",
                body="DCPS links additional compensation to teacher performance.",
                source_url="https://teacherquality.nctq.org/contract-database/district/dcps",  # type: ignore[arg-type]
                citation_status="ready",
            ),
            ExemplarPolicy(
                exemplar_id="exemplar:dallas-performance-pay",
                topic_id="differentiated-pay",
                district="Dallas ISD",
                district_id=26,
                subtopic="Performance pay",
                body="Dallas uses a performance-pay compensation structure.",
                source_url="https://teacherquality.nctq.org/contract-database/district/dallas",  # type: ignore[arg-type]
                citation_status="ready",
            ),
        ),
    )
    return PolicyGuidanceLibrary.build(topics={"differentiated-pay": topic})


def _ranking_result() -> ResultSet:
    return MetricRankingResult(
        rows=[
            RankingRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=70000.0,
                display_value="$70,000",
                academic_year="2024 - 2025",
                rank=1,
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$70,000",
                coverage_reason="answer_value",
            )
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Bravo District Contract, 2024-2025",
                url="https://example.org/bravo.pdf",
                page_number=2,
                page_ref="p. 2",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=2,
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
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback")
        ],
    )


def _sick_leave_peer_result() -> ResultSet:
    return PeerComparisonResult(
        selection=ResultSelection(
            scope="named_districts",
            districts=[
                SelectedDistrict(
                    district_id=26,
                    district_name="Denver Public Schools",
                    state="CO",
                ),
                SelectedDistrict(
                    district_id=24,
                    district_name="Aurora Public Schools",
                    state="CO",
                ),
            ],
        ),
        rows=[
            PeerComparisonRow(
                district_id=26,
                district_name="Denver Public Schools",
                state="CO",
                metric_id=198,
                metric_name="Maximum number of annual paid sick days",
                value="10",
                display_value="10",
                academic_year="2024 - 2025",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="10",
                coverage_reason="answer_value",
                peer_role="anchor",
                peer_reason="Anchor district selected by the user.",
            ),
            PeerComparisonRow(
                district_id=24,
                district_name="Aurora Public Schools",
                state="CO",
                metric_id=198,
                metric_name="Maximum number of annual paid sick days",
                value="12",
                display_value="12",
                academic_year="2024 - 2025",
                citation_markers=[2],
                coverage_state="covered",
                coverage_display="12",
                coverage_reason="answer_value",
                peer_role="peer",
                peer_rank=1,
                peer_score=0.86,
                peer_reason="Similar enrollment and same state.",
            ),
        ],
        citations=[
            CitationRef(marker=1, title="Denver Agreement", district_id=26),
            CitationRef(marker=2, title="Aurora Agreement", district_id=24),
        ],
        total_considered=2,
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=2,
            in_scope_count=2,
            addressed_count=2,
            real_data_count=2,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        order_statement=(
            "Compared selected policy metrics for the anchor district and "
            "1 deterministic NCES-similar peer district."
        ),
        methodology_codes=[
            MethodologyRef(code="peer_selection_nces_profiles"),
            MethodologyRef(code="peer_policy_cells_with_citations"),
        ],
    )


def _profile_ranking_result() -> ResultSet:
    return MetricRankingResult(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[
                SelectedDistrict(
                    district_id=2,
                    district_name="Bravo",
                    state="CA",
                )
            ],
        ),
        rows=[
            RankingRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=-1001,
                metric_name="NCES enrollment",
                value=70_000.0,
                display_value="70,000",
                academic_year="2024 - 2025",
                rank=1,
                source="profile_field",
                coverage_state="covered",
                coverage_display="70,000",
                coverage_reason="NCES directory year: 2023",
            )
        ],
        citations=[],
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
        order_statement="Ranked by NCES enrollment, highest to lowest.",
        source_notes=[],
        methodology_codes=[
            MethodologyRef(
                code="profile_rank_uses_profile_field",
                metadata={"profile_field": "enrollment"},
            )
        ],
    )


def _named_ranking_result() -> ResultSet:
    result = _ranking_result()
    return result.model_copy(
        update={
            "selection": ResultSelection(
                scope="named_districts",
                districts=[
                    SelectedDistrict(
                        district_id=2,
                        district_name="Bravo",
                        state="CA",
                    )
                ],
            )
        }
    )


def _state_lookup_result(state: str = "CA") -> ResultSet:
    return MetricLookupResult(        selection=ResultSelection(
            scope="state",
            states=[state],
            districts=[
                SelectedDistrict(
                    district_id=2,
                    district_name="Bravo",
                    state=state,
                )
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=2,
                district_name="Bravo",
                state=state,
                metric_id=1234,
                metric_name="Average teacher starting salary",
                value=70000.0,
                display_value="$70,000",
                academic_year="2024 - 2025",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="$70,000",
                coverage_reason="answer_value",
            )
        ],
        citations=[
            CitationRef(
                marker=1,
                title="Bravo District Contract, 2024-2025",
                url="https://example.org/bravo.pdf",
                page_number=2,
                page_ref="p. 2",
                academic_year="2024 - 2025",
                document_type="Contract",
                district_id=2,
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
        source_notes=[],
        methodology_codes=[MethodologyRef(code="lookup_default_district_order")],
        order_statement="Ordered by district name.",
    )


def _performance_pay_lookup_result(*, top_only: bool = False) -> ResultSet:
    rows = [
        MetricValueRow(
            district_id=44,
            district_name="Cumberland County Schools",
            state="NC",
            metric_id=501,
            metric_name="Maximum annual performance pay bonus, if eligible",
            value=10000.0,
            display_value="$10,000",
            academic_year="2024 - 2025",
            citation_markers=[1],
            coverage_state="covered",
            coverage_display="$10,000",
            coverage_reason="answer_value",
        ),
        MetricValueRow(
            district_id=60,
            district_name="Houston Independent School District",
            state="TX",
            metric_id=501,
            metric_name="Maximum annual performance pay bonus, if eligible",
            value=7680.0,
            display_value="$7,680",
            academic_year="2024 - 2025",
            citation_markers=[2],
            coverage_state="covered",
            coverage_display="$7,680",
            coverage_reason="answer_value",
        ),
    ]
    if top_only:
        rows = rows[:1]
    districts = [
        SelectedDistrict(
            district_id=row.district_id,
            district_name=row.district_name,
            state=row.state,
        )
        for row in rows
        if row.district_id is not None
    ]
    return MetricLookupResult(
        selection=ResultSelection(
            scope="named_districts" if top_only else "all_covered_districts",
            districts=districts,
        ),
        rows=rows,
        citations=[
            CitationRef(
                marker=1,
                title="Cumberland County Schools Performance Pay, 2024-2025",
                url="https://example.org/cumberland-performance-pay.pdf",
                page_number=8,
                page_ref="p. 8",
                academic_year="2024 - 2025",
                document_type="Policy Manual",
                district_id=44,
            ),
            CitationRef(
                marker=2,
                title="Houston ISD Compensation Manual, 2024-2025",
                url="https://example.org/houston-compensation.pdf",
                page_number=12,
                page_ref="p. 12",
                academic_year="2024 - 2025",
                document_type="Compensation Manual",
                district_id=60,
            ),
        ][: 1 if top_only else 2],
        total_considered=len(rows),
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=len(rows),
            in_scope_count=len(rows),
            addressed_count=len(rows),
            real_data_count=len(rows),
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        source_notes=[],
        methodology_codes=[MethodologyRef(code="lookup_default_district_order")],
        order_statement="Ordered by performance pay bonus, highest first.",
    )


def _strike_legality_lookup_result() -> ResultSet:
    return MetricLookupResult(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[
                SelectedDistrict(district_id=1, district_name="Alpha", state="CA"),
                SelectedDistrict(district_id=2, district_name="Bravo", state="CA"),
                SelectedDistrict(district_id=3, district_name="Maple", state="VT"),
            ],
        ),
        rows=[
            MetricValueRow(
                district_id=1,
                district_name="Alpha",
                state="CA",
                metric_id=262,
                metric_name="Legality of teacher strikes",
                value="Striking is permissible",
                display_value="Striking is permissible",
                academic_year="2024 - 2025",
                citation_markers=[1],
                coverage_state="covered",
                coverage_display="Striking is permissible",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=2,
                district_name="Bravo",
                state="CA",
                metric_id=262,
                metric_name="Legality of teacher strikes",
                value="Striking is permissible",
                display_value="Striking is permissible",
                academic_year="2024 - 2025",
                citation_markers=[2],
                coverage_state="covered",
                coverage_display="Striking is permissible",
                coverage_reason="answer_value",
            ),
            MetricValueRow(
                district_id=3,
                district_name="Maple",
                state="VT",
                metric_id=262,
                metric_name="Legality of teacher strikes",
                value="Striking is permissible",
                display_value="Striking is permissible",
                academic_year="2024 - 2025",
                citation_markers=[3],
                coverage_state="covered",
                coverage_display="Striking is permissible",
                coverage_reason="answer_value",
            ),
        ],
        citations=[
            CitationRef(marker=1, title="Alpha Contract", district_id=1),
            CitationRef(marker=2, title="Bravo Contract", district_id=2),
            CitationRef(marker=3, title="Maple Contract", district_id=3),
        ],
        total_considered=3,
        excluded_count=0,
        coverage_frame=CoverageFrame(
            universe_count=3,
            in_scope_count=3,
            addressed_count=3,
            real_data_count=3,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=1.0,
        ),
        source_notes=[],
        methodology_codes=[MethodologyRef(code="lookup_default_district_order")],
        order_statement="Ordered by state.",
    )


def test_fresh_chat_mvp_returns_direct_planner_turn() -> None:
    agent = _agent_for_turn(_direct_turn())

    with TestClient(
        create_app(planner_agent=agent, session_store=InMemorySessionStore())
    ) as client:
        response = client.post("/api/v1/chat/simple", json={"message": "hello"})

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Hello. I can help plan Compass questions."
    assert body["turn"]["route"] == "direct"
    assert body["turn"]["direct_response"]["reason"] == (
        "Greeting does not require data execution."
    )
    assert body["session"]["session_id"]
    assert body["session"]["turn_count"] == 1
    assert body["session"]["latest_snapshot_id"] == body["snapshot_id"]
    assert body["result"] is None
    assert body["validation"] is None
    assert body["manifest"] is None
    assert "trace_id" in body
    assert "logfire_url" in body
    assert "message_ids" in body


def test_fresh_chat_threads_runtime_into_verdict_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_fire_and_forget(
        response,
        *,
        pool=None,
        source=None,
        triggered_by="user_turn",
        user_message="",
    ) -> None:
        captured["pool"] = pool
        captured["source"] = source
        captured["user_message"] = user_message

    monkeypatch.setattr(
        "compass_backend.api.chat.verdict_pipeline.fire_and_forget",
        _fake_fire_and_forget,
    )
    agent = _agent_for_turn(_direct_turn())
    runtime_settings = Settings(session_store_backend="memory", pg_schema="tenant_schema")
    live_pool = object()

    with TestClient(
        create_app(
            planner_agent=agent,
            session_store=InMemorySessionStore(),
            app_settings=runtime_settings,
            chat_pool=live_pool,  # type: ignore[arg-type]
        )
    ) as client:
        response = client.post("/api/v1/chat/simple", json={"message": "hello"})

    assert response.status_code == 200
    assert captured["pool"] is live_pool
    source = captured["source"]
    assert isinstance(source, Settings)
    assert source.pg_schema == "tenant_schema"
    assert captured["user_message"] == "hello"


def test_fresh_chat_mvp_returns_execute_plan_with_result() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.86,
        query_plan=QueryPlan(
            question="Rank districts by starting salary.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting salary")],
            output=OutputSpec(format="table"),
        ),
    )
    agent = _agent_for_turn(turn)
    store = InMemorySessionStore()
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_ranking_result(),
            authority=ValidationAuthority(),
            message="Ranked by starting salary, highest to lowest.",
        )
    )

    with TestClient(
        create_app(
            planner_agent=agent,
            session_store=store,
            query_executor=executor,
        )
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={"message": "Rank all districts by starting salary."},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["message"].startswith(
        "I ranked covered districts by Average teacher starting salary, highest to lowest."
    )
    assert "| 1 | Bravo | CA | $70,000 | [1] |" in body["message"]
    assert body["turn"]["route"] == "execute"
    assert body["turn"]["query_plan"]["metrics"] == [
        {"name": "starting salary", "role": "primary", "degree_lane": None}
    ]
    assert body["result"]["result_type"] == "metric_ranking"
    assert body["result"]["rows"][0]["district_name"] == "Bravo"
    assert body["result"]["rows"][0]["rank"] == 1
    assert body["result"]["rows"][0]["citation_markers"] == [1]
    assert body["result"]["citations"][0]["marker"] == 1
    assert body["result"]["citations"][0]["title"] == (
        "Bravo District Contract, 2024-2025"
    )
    assert body["validation"]["valid"] is True
    assert body["validation"]["findings"] == []
    assert body["validation"]["dimensions_checked"] == [
        "selection",
        "metric",
        "sort_order",
        "surface_consistency",
        "coverage_state",
        "citation_coverage",
        "numeric_token_provenance",
        "fact_coverage",
        "coverage_wording",
    ]
    assert body["manifest"]["status"] == "rendered"
    assert body["manifest"]["body"] == body["message"]
    assert executor.plans == [turn.query_plan]

    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])
    assert snapshots[0].assistant_message == body["message"]
    assert snapshots[0].planner_turn == turn


def test_catalog_recall_shadow_attaches_manifest_and_trace_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.86,
        query_plan=QueryPlan(
            question="Rank districts by starting salary.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting salary")],
            output=OutputSpec(format="table"),
        ),
    )
    store = InMemorySessionStore()
    recall_service = FakeCatalogRecallService()
    span_attributes: list[dict[str, object]] = []

    def fake_set_span_attributes(_span, **attrs) -> None:
        span_attributes.append(attrs)

    monkeypatch.setattr(chat_module, "set_span_attributes", fake_set_span_attributes)

    async def run_response():
        return await build_chat_response(
            ChatRequest(message="Rank all districts by starting salary."),
            planner_agent=_agent_for_turn(turn),
            store=store,
            executor=FakeQueryExecutor(
                ExecutionSuccess(
                    result=_ranking_result(),
                    authority=ValidationAuthority(),
                    message="Ranked by starting salary, highest to lowest.",
                )
            ),
            app_settings=Settings(session_store_backend="memory"),
            catalog_recall_service=recall_service,  # type: ignore[arg-type]
        )

    response = anyio.run(run_response)

    assert [call["query"] for call in recall_service.calls] == [
        "Rank all districts by starting salary.",
        "starting salary",
    ]
    assert [call["expand_prompt"] for call in recall_service.calls] == [False, False]
    assert response.manifest is not None
    recall_metadata = response.manifest.metadata["catalog_recall"]
    assert recall_metadata["query"] == "Rank all districts by starting salary."
    assert len(recall_metadata["batches"]) == 2
    assert any(
        attrs.get("catalog_recall_shadow_enabled") is True
        and attrs.get("catalog_recall_request_count") == 2
        and attrs.get("catalog_recall_failure_count") == 0
        for attrs in span_attributes
    )
    assert any(
        attrs.get("catalog_recall_batch_count") == 2
        and attrs.get("catalog_recall_candidate_count") == 2
        and attrs.get("catalog_recall_methods") == ["metric_search"]
        for attrs in span_attributes
    )


def test_execution_recall_report_attaches_manifest_and_trace_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.86,
        query_plan=QueryPlan(
            question="Rank districts by starting salary.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting salary")],
            output=OutputSpec(format="table"),
        ),
    )
    recall_report = RecallReport(
        query="starting salary",
        batches=[
            RecallBatch(
                query="starting salary",
                entity_types=["metric"],
                candidates=[
                    CandidateCard(
                        input_phrase="starting salary",
                        entity_type="metric",
                        label="Starting salary",
                        source_methods=["metric_search"],
                        entity_ref="metric:89",
                    )
                ],
            )
        ],
    )
    span_attributes: list[dict[str, object]] = []

    def fake_set_span_attributes(_span, **attrs) -> None:
        span_attributes.append(attrs)

    monkeypatch.setattr(chat_module, "set_span_attributes", fake_set_span_attributes)

    async def run_response():
        return await build_chat_response(
            ChatRequest(message="Rank all districts by starting salary."),
            planner_agent=_agent_for_turn(turn),
            store=InMemorySessionStore(),
            executor=FakeQueryExecutor(
                ExecutionSuccess(
                    result=_ranking_result(),
                    authority=ValidationAuthority(),
                    message="Ranked by starting salary, highest to lowest.",
                    recall_report=recall_report,
                )
            ),
            app_settings=Settings(session_store_backend="memory"),
        )

    response = anyio.run(run_response)

    assert response.manifest is not None
    assert response.manifest.metadata["catalog_recall"]["query"] == "starting salary"
    assert any(
        attrs.get("catalog_recall_batch_count") == 1
        and attrs.get("catalog_recall_candidate_count") == 1
        and attrs.get("catalog_recall_methods") == ["metric_search"]
        for attrs in span_attributes
    )


def test_fresh_chat_converts_execution_metric_ambiguity_to_clarification() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.86,
        query_plan=QueryPlan(
            question="Show me the 5 districts that offer the most planning time.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="planning time")],
            limit={"count": 5, "kind": "top"},
            output=OutputSpec(format="table"),
        ),
    )
    clarification = ClarificationRequest(
        question=(
            'I found a few Compass metrics that could match "planning time". '
            "Do you mean one of these?"
        ),
        missing_fields=["metric"],
        candidates=[
            "Minimum amount of elementary teacher planning time per week (in minutes)",
            "Minimum amount of middle school teacher planning time per week (in minutes)",
            "Minimum amount of high school teacher planning time per week (in minutes)",
        ],
    )
    agent = _agent_for_turn(turn)
    store = InMemorySessionStore()
    executor = FakeQueryExecutor(
        ExecutionClarification(
            clarification=clarification,
            message=clarification.question,
        )
    )
    expected_message = (
        f"{clarification.question}\n\n"
        "- Minimum amount of elementary teacher planning time per week (in minutes)\n"
        "- Minimum amount of middle school teacher planning time per week (in minutes)\n"
        "- Minimum amount of high school teacher planning time per week (in minutes)"
    )

    with TestClient(
        create_app(
            planner_agent=agent,
            session_store=store,
            query_executor=executor,
        )
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={
                "message": "Show me the 5 districts that offer the most planning time."
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == expected_message
    assert body["turn"]["route"] == "clarify"
    assert body["turn"]["clarification"]["missing_fields"] == ["metric"]
    assert body["turn"]["clarification"]["candidates"] == clarification.candidates
    assert body["result"] is None
    assert body["validation"] is None
    assert body["manifest"] is None

    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])
    assert snapshots[0].planner_turn.route == "clarify"
    assert snapshots[0].planner_turn.clarification == clarification
    assert snapshots[0].assistant_message == expected_message


def test_build_chat_response_threads_trace_id_into_response_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "compass_backend.orchestration.chat.get_current_trace_id",
        lambda: "019ddbabf86e85dec07850e4835f4f18",
    )
    agent = _agent_for_turn(_direct_turn("Traceable response."))
    store = InMemorySessionStore()

    async def run_response():
        return await build_chat_response(
            ChatRequest(message="hello"),
            planner_agent=agent,
            store=store,
            executor=FakeQueryExecutor(ExecutionRefusal(message="unused direct executor")),
            app_settings=Settings(session_store_backend="memory"),
        )

    response = anyio.run(run_response)

    snapshots = anyio.run(store.snapshots_for_session, response.session.session_id)
    assert response.trace_id == "019ddbabf86e85dec07850e4835f4f18"
    assert response.logfire_url == (
        "https://logfire-us.pydantic.dev/murmuration/nctqai/live"
        "?traceId=019ddbabf86e85dec07850e4835f4f18"
    )
    assert response.message_ids == [2]
    assert snapshots[0].trace_id == response.trace_id


def test_planner_turn_validator_rejects_execute_with_blockers() -> None:
    """W1-01 (#834): defense in depth — constructing a PlannerTurn with
    route='execute' AND a non-empty recognition_blockers list must raise.
    This catches any future code path that tries to bypass the orchestration
    custody check."""

    with pytest.raises(ValidationError):
        PlannerTurn(
            route="execute",
            confidence=0.9,
            query_plan=QueryPlan(
                operation="rank",
                question="Rank by starting salary.",
                selection=SelectionSpec(scope="all_covered_districts"),
                metrics=[MetricSpec(name="starting salary")],
                output=OutputSpec(format="table"),
            ),
            recognition_blockers=[
                PlanningRecognitionMention(
                    phrase="starting salary",
                    entity_type="metric",
                    status="ambiguous",
                    recommended_action="clarify",
                )
            ],
        )


class _TraceContext:
    def __init__(self, trace_id: int, is_valid: bool = True) -> None:
        self.trace_id = trace_id
        self.is_valid = is_valid


class _TraceableTurnSpan:
    def __init__(self, trace_id: int) -> None:
        self._context = _TraceContext(trace_id)
        self.attributes: dict[str, object] = {}

    def __enter__(self) -> "_TraceableTurnSpan":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def get_span_context(self) -> _TraceContext:
        return self._context

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


def test_build_chat_response_falls_back_to_turn_span_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace_id = 0x19DDBABF86E85DEC07850E4835F4F18
    turn_span = _TraceableTurnSpan(trace_id)
    monkeypatch.setattr(
        "compass_backend.orchestration.chat.get_current_trace_id",
        lambda: None,
    )
    monkeypatch.setattr(
        "compass_backend.orchestration.chat.compass_turn_span",
        lambda **_: turn_span,
    )
    agent = _agent_for_turn(_direct_turn("Traceable response."))
    store = InMemorySessionStore()

    async def run_response():
        return await build_chat_response(
            ChatRequest(message="hello"),
            planner_agent=agent,
            store=store,
            executor=FakeQueryExecutor(ExecutionRefusal(message="unused direct executor")),
            app_settings=Settings(session_store_backend="memory"),
        )

    response = anyio.run(run_response)

    snapshots = anyio.run(store.snapshots_for_session, response.session.session_id)
    assert response.trace_id == "019ddbabf86e85dec07850e4835f4f18"
    assert response.logfire_url == (
        "https://logfire-us.pydantic.dev/murmuration/nctqai/live"
        "?traceId=019ddbabf86e85dec07850e4835f4f18"
    )
    assert snapshots[0].trace_id == response.trace_id
    assert turn_span.attributes["trace_id"] == response.trace_id


def test_chat_blocks_rendered_numeric_tokens_without_artifact_provenance(
    monkeypatch,
) -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.86,
        query_plan=QueryPlan(
            question="Rank districts by starting salary.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting salary")],
            output=OutputSpec(format="table"),
        ),
    )

    def fake_render_response(
        plan: QueryPlan,
        result: ResultSet,
        validation,
    ) -> ResponseManifest:
        if not validation.valid:
            codes = ", ".join(finding.code for finding in validation.findings)
            return ResponseManifest(
                body=f"Validation failed: {codes}.",
                status="validation_failed",
                result_type=result.result_type,
                validation_valid=False,
            )
        return ResponseManifest(
            body="Bravo has 999 teachers.",
            status="rendered",
            result_type=result.result_type,
            validation_valid=True,
        )

    monkeypatch.setattr("compass_backend.orchestration.chat.render_response", fake_render_response)
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_ranking_result(),
            authority=ValidationAuthority(),
            message="Ranked by starting salary, highest to lowest.",
        )
    )

    with TestClient(
        create_app(
            planner_agent=_agent_for_turn(turn),
            session_store=InMemorySessionStore(),
            query_executor=executor,
        )
    ) as client:
        response = client.post("/api/v1/chat/simple", json={"message": "Rank districts."})

    body = response.json()
    assert response.status_code == 200
    assert body["validation"]["valid"] is False
    # The fake renderer drops the table entirely while answer rows are
    # expected, so the #1514 Fix B table-absent guard
    # (markdown_row_count_mismatch) correctly fires alongside the
    # numeric-provenance guard this test targets.
    assert [finding["code"] for finding in body["validation"]["findings"]] == [
        "markdown_row_count_mismatch",
        "numeric_token_not_in_artifact",
    ]
    assert body["manifest"]["status"] == "validation_failed"
    assert "numeric_token_not_in_artifact" in body["message"]


def test_fresh_chat_mvp_serializes_named_district_selection_metadata() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.86,
        query_plan=QueryPlan(
            question="Rank Bravo by starting salary.",
            selection=SelectionSpec(scope="named_districts", districts=["Bravo"]),
            metrics=[MetricSpec(name="starting salary")],
            output=OutputSpec(format="table"),
        ),
    )
    agent = _agent_for_turn(turn)
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_named_ranking_result(),
            authority=ValidationAuthority(),
            message="Ranked selected districts by starting salary.",
        )
    )

    with TestClient(
        create_app(
            planner_agent=agent,
            query_executor=executor,
            session_store=InMemorySessionStore(),
        )
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={"message": "Rank Bravo by starting salary."},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["message"].startswith(
        "I ranked selected districts by Average teacher starting salary, highest to lowest."
    )
    assert body["result"]["selection"]["scope"] == "named_districts"
    assert body["result"]["selection"]["districts"] == [
        {"district_id": 2, "district_name": "Bravo", "state": "CA"}
    ]
    assert body["validation"]["valid"] is True
    assert body["manifest"]["status"] == "rendered"


def test_fresh_chat_mvp_returns_clarification_question() -> None:
    turn = PlannerTurn(
        route="clarify",
        confidence=0.75,
        clarification=ClarificationRequest(
            question="Which district should I use?",
            missing_fields=["district"],
        ),
    )
    agent = _agent_for_turn(turn)

    with TestClient(
        create_app(planner_agent=agent, session_store=InMemorySessionStore())
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={"message": "What is starting salary?"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Which district should I use?"
    assert body["turn"]["route"] == "clarify"
    assert body["turn"]["clarification"]["missing_fields"] == ["district"]
    assert body["result"] is None
    assert body["validation"] is None
    assert body["manifest"] is None


def test_fresh_chat_converts_planner_structured_output_failure_to_clarification() -> None:
    agent = Agent(
        TestModel(
            custom_output_args={
                "route": "clarify",
                "confidence": 0.4,
                "clarification": '{"question":"Which district group?"}',
            },
            # #1248: don't auto-invoke the always-attached catalog toolset; the
            # offline ChatPoolHolder has no DB for a tool call to reach.
            call_tools=[],
        ),
        output_type=PlannerTurn,
        retries={"output": 1},
    )
    store = InMemorySessionStore()

    with TestClient(
        create_app(planner_agent=agent, session_store=store),
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={"message": "compare salary"},
        )

    assert response.status_code == 200
    body = response.json()
    # The post-planner rescue modules are retired. Structured-output exhaustion
    # now stays a typed clarification path; the shape guard enriches the generic
    # fallback so users still get a concrete rephrase template.
    assert body["message"] != (
        "I could not structure that request safely. Can you choose the "
        "district group and metric you want me to compare?"
    )
    # The new message names at least one supported query pattern so
    # the user has a concrete rephrase template.
    assert (
        "top 10 districts" in body["message"]
        or "compare" in body["message"]
    )
    assert body["turn"]["route"] == "clarify"
    assert body["turn"]["clarification"]["missing_fields"] == [
        "comparison_group",
        "metric",
    ]
    assert body["result"] is None
    assert body["validation"] is None
    assert body["manifest"] is None

    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])
    assert len(snapshots) == 1
    assert snapshots[0].assistant_message == body["message"]
    assert snapshots[0].planner_turn.route == "clarify"
    # PendingQueryContext is still emitted (typed-clarification contract).
    assert snapshots[0].pending_query_context is not None


def test_structured_output_failure_recovers_real_performance_pay_threshold() -> None:
    agent = StructuredFailurePlannerAgent()
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_performance_pay_lookup_result(),
            authority=ValidationAuthority(),
            message="Performance pay rows.",
        )
    )

    with TestClient(
        create_app(
            planner_agent=agent,
            query_executor=executor,
            session_store=InMemorySessionStore(),
        ),
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={
                "message": (
                    "I want to find districts with real performance pay - not "
                    "just token bonuses."
                )
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["turn"]["route"] == "execute"
    assert len(executor.plans) == 1
    plan = executor.plans[0]
    assert plan.operation == "lookup"
    assert plan.metrics == [
        MetricSpec(name="Maximum annual performance pay bonus, if eligible")
    ]
    assert plan.filters == [
        FilterSpec(
            field="Maximum annual performance pay bonus, if eligible",
            operator="greater_than_or_equal",
            value=5000,
            threshold_hint="real (not token)",
        )
    ]
    assert plan.sort == SortSpec(
        field="Maximum annual performance pay bonus, if eligible",
        direction="desc",
    )


def test_structured_output_failure_recovers_performance_pay_inventory() -> None:
    agent = StructuredFailurePlannerAgent()
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_performance_pay_lookup_result(),
            authority=ValidationAuthority(),
            message="Performance pay rows.",
        )
    )

    with TestClient(
        create_app(
            planner_agent=agent,
            query_executor=executor,
            session_store=InMemorySessionStore(),
        ),
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={"message": "what data do you have about performance pay?"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["turn"]["route"] == "direct"
    assert executor.plans == []
    assert "Minimum annual performance pay bonus" in body["message"]
    assert "Maximum annual performance pay bonus" in body["message"]
    assert "rank" in body["message"]


def test_fresh_chat_runs_strike_legality_followup_after_policy_guidance() -> None:
    reset_library()
    set_library(_toy_policy_guidance_library())
    try:
        planner = SequencePlannerAgent(
            [
                PlannerTurn(
                    route="policy_guidance",
                    confidence=0.87,
                    policy_guidance=PolicyGuidancePlan(
                        topic_ids=["runtime-topic"],
                        layers=["stances"],
                        intent_summary="User asks for NCTQ's stance on strike legality.",
                        focus_terms=["teacher strikes"],
                    ),
                ),
                PlannerTurn(
                    route="execute",
                    confidence=0.82,
                    query_plan=QueryPlan(
                        operation="lookup",
                        question="Which states allow strikes?",
                        selection=SelectionSpec(scope="all_covered_districts"),
                        metrics=[MetricSpec(name="Legality of teacher strikes")],
                        filters=[
                            FilterSpec(
                                field="Legality of teacher strikes",
                                operator="equals",
                                value="Striking is permissible",
                            )
                        ],
                        output=OutputSpec(
                            format="table",
                            row_display="all",
                            group_by="state",
                        ),
                    ),
                ),
            ]
        )
        store = InMemorySessionStore()
        executor = SequenceQueryExecutor(
            [
                ExecutionSuccess(
                    result=_strike_legality_lookup_result(),
                    authority=ValidationAuthority(),
                    message="Looked up strike legality by state.",
                )
            ]
        )

        with TestClient(
            create_app(
                planner_agent=planner,
                session_store=store,
                query_executor=executor,
            )
        ) as client:
            first_response = client.post(
                "/api/v1/chat/simple",
                json={
                    "message": (
                        "What is NCTQ's position on the legality of teacher strikes?"
                    )
                },
            )
            session_id = first_response.json()["session"]["session_id"]
            second_response = client.post(
                "/api/v1/chat/simple",
                json={
                    "message": "Which states allow strikes?",
                    "session_id": session_id,
                },
            )
    finally:
        reset_library()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    body = second_response.json()
    assert body["turn"]["route"] == "execute"
    assert executor.plans
    executed = executor.plans[0]
    strike_metric = "Legality of teacher strikes"
    assert executed.operation == "lookup"
    assert executed.selection == SelectionSpec(scope="all_covered_districts")
    assert executed.metrics == [MetricSpec(name=strike_metric)]
    assert executed.filters == [
        FilterSpec(
            field=strike_metric,
            operator="equals",
            value="Striking is permissible",
        )
    ]
    assert executed.output == OutputSpec(
        format="table",
        row_display="all",
        group_by="state",
    )
    assert "Based on NCTQ's covered district data" in body["message"]
    assert "| CA | 2 | 2024 - 2025 | Striking is permissible | [1] [2] |" in body["message"]
    assert "does not cover state strike legality" not in body["message"]


def test_policy_guidance_detail_followup_uses_stored_exemplar_context() -> None:
    reset_library()
    set_library(_performance_pay_policy_guidance_library())
    try:
        planner = SequencePlannerAgent(
            [
                PlannerTurn(
                    route="policy_guidance",
                    confidence=0.91,
                    policy_guidance=PolicyGuidancePlan(
                        topic_ids=["differentiated-pay"],
                        layers=["exemplars"],
                        intent_summary="User asked for performance pay exemplars.",
                        focus_terms=["performance pay"],
                    ),
                )
            ]
        )
        store = InMemorySessionStore()
        executor = SequenceQueryExecutor([])

        with TestClient(
            create_app(
                planner_agent=planner,
                session_store=store,
                query_executor=executor,
            )
        ) as client:
            first_response = client.post(
                "/api/v1/chat/simple",
                json={"message": "Which districts have strong performance pay?"},
            )
            session_id = first_response.json()["session"]["session_id"]
            second_response = client.post(
                "/api/v1/chat/simple",
                json={
                    "message": "Now show me actual policy details for the top one.",
                    "session_id": session_id,
                },
            )
    finally:
        reset_library()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert planner.prompts == ["Which districts have strong performance pay?"]
    assert executor.plans == []

    first_snapshots = anyio.run(
        store.snapshots_for_session,
        first_response.json()["session"]["session_id"],
    )
    assert first_snapshots[0].memory is not None
    assert first_snapshots[0].memory.latest_query_context is None
    assert first_snapshots[0].memory.latest_policy_guidance_context is not None
    assert [
        ref.exemplar_id
        for ref in first_snapshots[0].memory.latest_policy_guidance_context.exemplars
    ] == [
        "exemplar:dcps-performance-pay",
        "exemplar:dallas-performance-pay",
    ]

    body = second_response.json()
    assert body["turn"]["route"] == "policy_guidance"
    assert body["turn"]["policy_guidance"]["response_mode"] == "exemplar_detail"
    assert body["turn"]["policy_guidance"]["selected_exemplar_ids"] == [
        "exemplar:dcps-performance-pay"
    ]
    assert "District of Columbia Public Schools" in body["message"]
    assert "DCPS links additional compensation to teacher performance." in body["message"]
    assert "Dallas ISD" not in body["message"]
    assert body["manifest"]["metadata"]["response_mode"] == "exemplar_detail"
    assert body["manifest"]["metadata"]["selected_exemplar_ids"] == [
        "exemplar:dcps-performance-pay"
    ]
    assert body["manifest"]["metadata"]["citations"][0]["url"].endswith(
        "/district/dcps"
    )


def test_policy_guidance_data_followup_still_goes_to_planner() -> None:
    reset_library()
    set_library(_performance_pay_policy_guidance_library())
    try:
        planner = SequencePlannerAgent(
            [
                PlannerTurn(
                    route="policy_guidance",
                    confidence=0.91,
                    policy_guidance=PolicyGuidancePlan(
                        topic_ids=["differentiated-pay"],
                        layers=["exemplars"],
                        intent_summary="User asked for performance pay exemplars.",
                        focus_terms=["performance pay"],
                    ),
                ),
                PlannerTurn(
                    route="clarify",
                    confidence=0.74,
                    clarification=ClarificationRequest(
                        question=(
                            "Which performance-pay metric should I use for bonus "
                            "amounts?"
                        ),
                        missing_fields=["metric"],
                        candidates=[],
                    ),
                ),
            ]
        )
        store = InMemorySessionStore()
        executor = SequenceQueryExecutor([])

        with TestClient(
            create_app(
                planner_agent=planner,
                session_store=store,
                query_executor=executor,
            )
        ) as client:
            first_response = client.post(
                "/api/v1/chat/simple",
                json={"message": "Which districts have strong performance pay?"},
            )
            session_id = first_response.json()["session"]["session_id"]
            second_response = client.post(
                "/api/v1/chat/simple",
                json={
                    "message": "Make a table of min and max bonus amounts for the top one.",
                    "session_id": session_id,
                },
            )
    finally:
        reset_library()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert planner.prompts == [
        "Which districts have strong performance pay?",
        "Make a table of min and max bonus amounts for the top one.",
    ]
    assert second_response.json()["turn"]["route"] == "clarify"
    assert executor.plans == []


def test_data_detail_followup_uses_prior_top_district_and_metric() -> None:
    first_plan = QueryPlan(
        operation="lookup",
        question="Find districts with real performance pay.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="Maximum annual performance pay bonus, if eligible")],
        sort=SortSpec(
            field="Maximum annual performance pay bonus, if eligible",
            direction="desc",
        ),
        limit=LimitSpec(kind="all"),
        output=OutputSpec(format="table"),
    )
    broad_detail_plan = QueryPlan(
        operation="lookup",
        question="Show policy details for the top one.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Cumberland County Schools"],
        ),
        metrics=[
            MetricSpec(name="District offers additional pay for hard-to-staff schools"),
            MetricSpec(name="District offers additional pay for hard-to-staff subjects"),
            MetricSpec(name="Specific subjects and areas eligible for additional pay"),
        ],
        output=OutputSpec(format="table"),
    )
    planner = SequencePlannerAgent(
        [
            PlannerTurn(route="execute", confidence=0.93, query_plan=first_plan),
            PlannerTurn(route="execute", confidence=0.86, query_plan=broad_detail_plan),
        ]
    )
    store = InMemorySessionStore()
    executor = SequenceQueryExecutor(
        [
            ExecutionSuccess(
                result=_performance_pay_lookup_result(),
                authority=ValidationAuthority(),
                message="Performance pay rows.",
            ),
            ExecutionSuccess(
                result=_performance_pay_lookup_result(top_only=True),
                authority=ValidationAuthority(),
                message="Top policy detail row.",
            ),
        ]
    )

    with TestClient(
        create_app(
            planner_agent=planner,
            session_store=store,
            query_executor=executor,
        )
    ) as client:
        first_response = client.post(
            "/api/v1/chat/simple",
            json={
                "message": (
                    "I want to find districts with real performance pay - not "
                    "just token bonuses."
                )
            },
        )
        session_id = first_response.json()["session"]["session_id"]
        second_response = client.post(
            "/api/v1/chat/simple",
            json={
                "message": "Now show me the actual policy details for the top one.",
                "session_id": session_id,
            },
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert planner.prompts == [
        "I want to find districts with real performance pay - not just token bonuses."
    ]
    assert len(executor.plans) == 2
    followup_plan = executor.plans[1]
    assert followup_plan.operation == "lookup"
    assert followup_plan.selection == SelectionSpec(
        scope="named_districts",
        districts=["Cumberland County Schools"],
    )
    assert followup_plan.metrics == [
        MetricSpec(name="Maximum annual performance pay bonus, if eligible")
    ]
    assert followup_plan.sort is None
    assert followup_plan.limit is None
    assert "Houston Independent School District" not in second_response.json()["message"]


def test_fresh_chat_structured_option_click_resumes_without_planner() -> None:
    """#1348: clicking a grounded clarify option resumes deterministically.

    Turn 1 routes execute on an ambiguous district; execution returns a
    clarification carrying structured options (which persist to memory).
    Turn 2 posts `selected_option` — the orchestration short-circuits the
    planner, injects the chosen grounded district, and executes.
    """
    execute_turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="lookup",
            question="What are evaluation policies in Cleveland County?",
            selection=SelectionSpec(
                scope="named_districts", districts=["Cleveland County"]
            ),
            metrics=[MetricSpec(name="teacher evaluation policy")],
        ),
    )
    # A second planner turn that should NEVER be consumed once the click
    # short-circuits the planner — its presence makes the RED state a clean
    # assertion failure (route=clarify / planner called twice) rather than an
    # IndexError from an exhausted sequence.
    fallback_clarify = PlannerTurn(
        route="clarify",
        confidence=0.5,
        clarification=ClarificationRequest(
            question="Which district did you mean?",
            missing_fields=["district"],
            pending_context=PendingQueryContext(
                operation="lookup",
                metrics=[MetricSpec(name="teacher evaluation policy")],
                missing_fields=["district"],
            ),
        ),
    )
    planner = SequencePlannerAgent([execute_turn, fallback_clarify])
    clarification = ExecutionClarification(
        clarification=ClarificationRequest(
            question='I found more than one covered district matching "Cleveland County".',
            missing_fields=["district"],
            candidates=["Cleveland County Schools, NC", "Cleveland County, OK"],
            candidate_options=[
                ClarificationOption(value="2700", label="Cleveland County Schools, NC"),
                ClarificationOption(value="4011", label="Cleveland County, OK"),
            ],
        ),
        message='I found more than one covered district matching "Cleveland County".',
    )
    success = ExecutionSuccess(
        result=_state_lookup_result("NC"),
        authority=ValidationAuthority(),
        message="Cleveland County Schools evaluation policy.",
    )
    executor = SequenceQueryExecutor([clarification, success])
    store = InMemorySessionStore()

    with TestClient(
        create_app(
            planner_agent=planner,
            session_store=store,
            query_executor=executor,
        )
    ) as client:
        first = client.post(
            "/api/v1/chat/simple",
            json={"message": "What are evaluation policies in Cleveland County?"},
        )
        session_id = first.json()["session"]["session_id"]
        second = client.post(
            "/api/v1/chat/simple",
            json={
                "message": "Cleveland County Schools, NC",
                "session_id": session_id,
                "selected_option": "2700",
            },
        )

    assert first.status_code == 200
    assert first.json()["turn"]["route"] == "clarify"

    assert second.status_code == 200
    second_body = second.json()
    assert second_body["turn"]["route"] == "execute"
    # The clicked district resumed deterministically into the executed plan.
    assert second_body["turn"]["query_plan"]["selection"]["districts"] == [
        "Cleveland County Schools"
    ]
    assert second_body["turn"]["query_plan"]["selection"]["states"] == ["NC"]
    # The original typed intent (the metric) is preserved through the resume.
    assert second_body["turn"]["query_plan"]["metrics"] == [
        {"name": "teacher evaluation policy", "role": "primary", "degree_lane": None}
    ]
    # The planner ran ONLY for turn 1 — the click skipped it entirely.
    assert len(planner.prompts) == 1
    # The executor's second plan carries the resolved district + state filter.
    assert executor.plans[1].selection.districts == ["Cleveland County Schools"]
    assert executor.plans[1].selection.states == ["NC"]


def test_fresh_chat_unknown_selected_option_falls_through_to_planner() -> None:
    """A stale/forged option id is ignored — the planner re-clarifies safely."""
    execute_turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="lookup",
            question="What are evaluation policies in Cleveland County?",
            selection=SelectionSpec(
                scope="named_districts", districts=["Cleveland County"]
            ),
            metrics=[MetricSpec(name="teacher evaluation policy")],
        ),
    )
    replan_clarify = PlannerTurn(
        route="clarify",
        confidence=0.5,
        clarification=ClarificationRequest(
            question="Which district did you mean?",
            missing_fields=["district"],
            pending_context=PendingQueryContext(
                operation="lookup",
                metrics=[MetricSpec(name="teacher evaluation policy")],
                missing_fields=["district"],
            ),
        ),
    )
    planner = SequencePlannerAgent([execute_turn, replan_clarify])
    clarification = ExecutionClarification(
        clarification=ClarificationRequest(
            question="Which district do you mean?",
            missing_fields=["district"],
            candidates=["Cleveland County Schools, NC"],
            candidate_options=[
                ClarificationOption(value="2700", label="Cleveland County Schools, NC"),
            ],
        ),
        message="Which district do you mean?",
    )
    # Two outcomes: turn 1 clarifies; a forged id does NOT short-circuit, so
    # turn 2 proceeds normally (planner runs) and re-clarifies.
    executor = SequenceQueryExecutor([clarification, clarification])
    store = InMemorySessionStore()

    with TestClient(
        create_app(
            planner_agent=planner,
            session_store=store,
            query_executor=executor,
        )
    ) as client:
        first = client.post(
            "/api/v1/chat/simple",
            json={"message": "What are evaluation policies in Cleveland County?"},
        )
        session_id = first.json()["session"]["session_id"]
        second = client.post(
            "/api/v1/chat/simple",
            json={
                "message": "the first one",
                "session_id": session_id,
                "selected_option": "9999",  # never offered
            },
        )

    assert second.status_code == 200
    # The forged id did NOT deterministically resume — the planner ran on
    # turn 2 (short-circuit skipped), and no forged district was injected.
    assert len(planner.prompts) == 2
    for plan in executor.plans:
        assert "9999" not in plan.selection.districts


def test_fresh_chat_persists_pending_context_for_clarification_chain() -> None:
    pending_metric = PendingQueryContext(
        operation="lookup",
        metrics=[MetricSpec(name="salary metrics")],
        missing_fields=["district", "scope"],
    )
    pending_selection = PendingQueryContext(
        selection=SelectionSpec(scope="state", states=["NC"]),
        missing_fields=["metric"],
    )
    planner = SequencePlannerAgent(
        [
            PlannerTurn(
                route="clarify",
                confidence=0.75,
                clarification=ClarificationRequest(
                    question="Which districts should I use?",
                    missing_fields=["district", "scope"],
                    pending_context=pending_metric,
                ),
            ),
            PlannerTurn(
                route="clarify",
                confidence=0.75,
                clarification=ClarificationRequest(
                    question="What information should I show?",
                    missing_fields=["metric"],
                    pending_context=pending_selection,
                ),
            ),
        ]
    )
    store = InMemorySessionStore()
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_state_lookup_result("NC"),
            authority=ValidationAuthority(),
            message="North Carolina salary lookup.",
        )
    )

    with TestClient(
        create_app(
            planner_agent=planner,
            session_store=store,
            query_executor=executor,
        )
    ) as client:
        first_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "show me all salary metrics"},
        )
        session_id = first_response.json()["session"]["session_id"]
        second_response = client.post(
            "/api/v1/chat/simple",
            json={
                "message": "all districts in north carolina",
                "session_id": session_id,
            },
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_body = first_response.json()
    second_body = second_response.json()
    assert "pending_query_context" not in first_body["session"]
    assert "pending_query_context" not in second_body["session"]
    assert second_body["turn"]["route"] == "execute"
    assert second_body["turn"]["query_plan"]["selection"] == {
        "scope": "state",
        "districts": [],
        "states": ["NC"],
    }
    assert second_body["turn"]["query_plan"]["metrics"] == [
        {"name": "salary metrics", "role": "primary", "degree_lane": None}
    ]
    assert executor.plans[0].selection.states == ["NC"]
    assert executor.plans[0].metrics == [MetricSpec(name="salary metrics")]

    snapshots = anyio.run(store.snapshots_for_session, session_id)
    assert snapshots[0].pending_query_context is not None
    assert snapshots[0].pending_query_context.metrics == [
        MetricSpec(name="salary metrics")
    ]
    assert snapshots[1].pending_query_context is None
    reloaded = anyio.run(store.load, session_id)
    assert reloaded.pending_query_context is None
    assert reloaded.query_context is not None


def test_fresh_chat_pending_context_retains_prior_state_when_metric_arrives() -> None:
    planner = SequencePlannerAgent(
        [
            PlannerTurn(
                route="clarify",
                confidence=0.74,
                clarification=ClarificationRequest(
                    question="Which salary metric should I use?",
                    missing_fields=["metric"],
                    pending_context=PendingQueryContext(
                        operation="lookup",
                        selection=SelectionSpec(scope="state", states=["CA"]),
                        missing_fields=["metric"],
                    ),
                ),
            ),
            PlannerTurn(
                route="clarify",
                confidence=0.74,
                clarification=ClarificationRequest(
                    question="Which districts should I use?",
                    missing_fields=["district", "scope"],
                    pending_context=PendingQueryContext(
                        metrics=[MetricSpec(name="starting salary")],
                        missing_fields=["district", "scope"],
                    ),
                ),
            ),
        ]
    )
    store = InMemorySessionStore()
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_state_lookup_result("CA"),
            authority=ValidationAuthority(),
            message="California salary lookup.",
        )
    )

    with TestClient(
        create_app(
            planner_agent=planner,
            session_store=store,
            query_executor=executor,
        )
    ) as client:
        first_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "how much does california pay teachers?"},
        )
        session_id = first_response.json()["session"]["session_id"]
        second_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "salary", "session_id": session_id},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["turn"]["route"] == "execute"
    promoted_plan = executor.plans[0]
    assert promoted_plan.selection == SelectionSpec(scope="state", states=["CA"])
    assert promoted_plan.metrics == [MetricSpec(name="starting salary")]


# Three tests deleted (Tier-2 cut for #1057): they exercised the deleted
# prose-dispatch helpers `normalize_salary_rank_pending_context` and the
# `message=`-driven combined-table gate. The recipe-shaped behavior lives
# in `instructions/planner_guidance/teacher-compensation-salary.md` (merged via
# #1041). Refs #1059 #1060.
def test_ba_ma_salary_rank_execute_plan_defaults_to_composite_rankings() -> None:
    from compass_backend.planning.planner import normalize_ba_ma_salary_rank_turn

    turn = PlannerTurn(
        route="execute",
        confidence=0.95,
        query_plan=QueryPlan(
            operation="rank",
            question="starting salary for teachers with a BA and teachers with an MA",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[
                MetricSpec(name="starting teacher salary", degree_lane="ba"),
                MetricSpec(
                    name="starting teacher salary",
                    role="comparison",
                    degree_lane="ma",
                ),
            ],
            limit=LimitSpec(count=10, kind="top"),
        ),
    )

    normalized = normalize_ba_ma_salary_rank_turn(turn)

    assert normalized.query_plan is not None
    assert normalized.query_plan.requires_composite_ranking is True
    assert [metric.role for metric in normalized.query_plan.metrics] == [
        "primary",
        "primary",
    ]


def test_fresh_chat_execution_clarification_persists_metric_candidates() -> None:
    candidate_names = [
        "Does the district cover 100% of employees' health insurance premium?",
        "What percent of the employees' health insurance premium does the district cover?",
        (
            "Maximum portion of the employee's dependents' health insurance "
            "premium paid by the employer"
        ),
        "Dollar cap for portion of health insurance premium covered by employer",
    ]
    planner = SequencePlannerAgent(
        [
            PlannerTurn(
                route="execute",
                confidence=0.72,
                query_plan=QueryPlan(
                    operation="rank",
                    question=(
                        "What are the ten districts that provide the most "
                        "meaningful benefits?"
                    ),
                    selection=SelectionSpec(scope="all_covered_districts"),
                    metrics=[MetricSpec(name="benefits")],
                    sort=SortSpec(field="benefits", direction="desc"),
                    limit=LimitSpec(count=10, kind="top"),
                ),
            )
        ]
    )
    store = InMemorySessionStore()
    executor = FakeQueryExecutor(
        ExecutionClarification(
            clarification=ClarificationRequest(
                question='I found a few Compass metrics that could match "benefits".',
                missing_fields=["metric"],
                candidates=candidate_names,
            ),
            message='I found a few Compass metrics that could match "benefits".',
        )
    )

    with TestClient(
        create_app(
            planner_agent=planner,
            session_store=store,
            query_executor=executor,
        )
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={
                "message": (
                    "What are the ten districts that provide the most "
                    "meaningful benefits"
                )
            },
        )

    assert response.status_code == 200
    session_id = response.json()["session"]["session_id"]
    snapshots = anyio.run(store.snapshots_for_session, session_id)
    assert snapshots[0].pending_query_context is not None
    assert snapshots[0].pending_query_context.metrics == [
        MetricSpec(name=name) for name in candidate_names
    ]
    assert snapshots[0].pending_query_context.missing_fields == ["metric"]


def test_fresh_chat_metric_option_click_resumes_to_one_metric() -> None:
    """#1348 regression: clicking a metric clarify option ranks exactly that metric.

    The reported prompt ("...the most planning time") is metric-ambiguous: turn 1
    routes execute, execution returns a metric clarification carrying the three
    grounded level options (built at _clarify_helpers), which persist to memory
    with the pending plan (selection/sort/limit + ALL candidate metrics). Turn 2
    posts the clicked option's value (the canonical metric name); resume must
    REPLACE the metric set with the one chosen — never inject (which would rank
    all three) and never reconstruct a bogus named_districts selection from the
    label (the latent district-only-resume bug this generalizes away).
    """
    levels = [
        "Minimum amount of elementary teacher planning time per week (in minutes)",
        "Minimum amount of middle school teacher planning time per week (in minutes)",
        "Minimum amount of high school teacher planning time per week (in minutes)",
    ]
    execute_turn = PlannerTurn(
        route="execute",
        confidence=0.8,
        query_plan=QueryPlan(
            operation="rank",
            question="Show me the 5 districts that offer the most planning time",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="planning time")],
            sort=SortSpec(field="planning time", direction="desc"),
            limit=LimitSpec(count=5, kind="top"),
        ),
    )
    # Never consumed once the click short-circuits the planner — its presence
    # turns a RED into a clean assertion failure rather than an IndexError.
    fallback_clarify = PlannerTurn(
        route="clarify",
        confidence=0.5,
        clarification=ClarificationRequest(
            question="Which planning-time metric?",
            missing_fields=["metric"],
            pending_context=PendingQueryContext(
                operation="rank",
                selection=SelectionSpec(scope="all_covered_districts"),
                missing_fields=["metric"],
            ),
        ),
    )
    planner = SequencePlannerAgent([execute_turn, fallback_clarify])
    clarification = ExecutionClarification(
        clarification=ClarificationRequest(
            question='I found a few Compass metrics that could match "planning time".',
            missing_fields=["metric"],
            candidates=levels,
            candidate_options=[
                ClarificationOption(value=name, label=name, detail="Planning time")
                for name in levels
            ],
        ),
        message='I found a few Compass metrics that could match "planning time".',
    )
    success = ExecutionSuccess(
        result=_ranking_result(),
        authority=ValidationAuthority(),
        message="Top 5 districts by middle-school planning time.",
    )
    executor = SequenceQueryExecutor([clarification, success])
    store = InMemorySessionStore()

    with TestClient(
        create_app(
            planner_agent=planner,
            session_store=store,
            query_executor=executor,
        )
    ) as client:
        first = client.post(
            "/api/v1/chat/simple",
            json={"message": "Show me the 5 districts that offer the most planning time"},
        )
        session_id = first.json()["session"]["session_id"]
        second = client.post(
            "/api/v1/chat/simple",
            json={
                "message": levels[1],
                "session_id": session_id,
                "selected_option": levels[1],  # the middle-school option's value
            },
        )

    assert first.status_code == 200
    assert first.json()["turn"]["route"] == "clarify"

    assert second.status_code == 200
    second_body = second.json()
    assert second_body["turn"]["route"] == "execute"
    plan = second_body["turn"]["query_plan"]
    # Exactly the one clicked metric — not all three (REPLACE, not inject).
    assert [m["name"] for m in plan["metrics"]] == [levels[1]]
    # The clicked metric did NOT become a district selection (the latent bug).
    assert plan["selection"]["scope"] == "all_covered_districts"
    assert plan["selection"]["districts"] == []
    # The rest of the pending plan rode through the resume untouched.
    assert plan["limit"]["count"] == 5
    # The planner ran ONLY for turn 1 — the click skipped it entirely.
    assert len(planner.prompts) == 1
    assert executor.plans[1].metrics == [MetricSpec(name=levels[1])]
    assert executor.plans[1].selection.scope == "all_covered_districts"


def test_fresh_chat_metric_click_with_metric_value_filter_defers_to_planner() -> None:
    """#1348 B1 guard: don't deterministically resume when the ambiguous metric
    phrase is mirrored in a metric_value FilterSpec.

    A threshold query carries the ambiguous phrase in BOTH ``metrics`` and a
    ``kind="metric_value"`` filter field. REPLACE rewrites only ``metrics``, so
    the filter would stay keyed to the stale phrase, execution would re-resolve
    it, re-clarify, and the click would loop. The resume must instead defer to
    the planner (which re-authors a consistent metric+filter pair). Contrast with
    test_fresh_chat_metric_option_click_resumes_to_one_metric, where the same
    click with NO metric_value filter DOES resume deterministically.
    """
    levels = [
        "Minimum amount of elementary teacher planning time per week (in minutes)",
        "Minimum amount of middle school teacher planning time per week (in minutes)",
        "Minimum amount of high school teacher planning time per week (in minutes)",
    ]
    metric_value_filter = FilterSpec(
        field="planning time",
        operator="greater_than",
        value=200,
        kind="metric_value",
    )
    execute_turn = PlannerTurn(
        route="execute",
        confidence=0.8,
        query_plan=QueryPlan(
            operation="rank",
            question="Rank districts offering more than 200 minutes of planning time",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="planning time")],
            filters=[metric_value_filter],
            sort=SortSpec(field="planning time", direction="desc"),
        ),
    )
    # On the click turn the guard returns False, so the planner runs again and
    # re-plans from the clicked metric name into a consistent metric+filter pair.
    replan_execute = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="rank",
            question="Rank districts offering more than 200 minutes of planning time",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name=levels[2])],
            filters=[
                FilterSpec(
                    field=levels[2],
                    operator="greater_than",
                    value=200,
                    kind="metric_value",
                )
            ],
            sort=SortSpec(field=levels[2], direction="desc"),
        ),
    )
    planner = SequencePlannerAgent([execute_turn, replan_execute])
    clarification = ExecutionClarification(
        clarification=ClarificationRequest(
            question='I found a few Compass metrics that could match "planning time".',
            missing_fields=["metric"],
            candidates=levels,
            candidate_options=[
                ClarificationOption(value=name, label=name) for name in levels
            ],
        ),
        message='I found a few Compass metrics that could match "planning time".',
    )
    success = ExecutionSuccess(
        result=_ranking_result(),
        authority=ValidationAuthority(),
        message="Ranked districts above the planning-time threshold.",
    )
    executor = SequenceQueryExecutor([clarification, success])
    store = InMemorySessionStore()

    with TestClient(
        create_app(
            planner_agent=planner,
            session_store=store,
            query_executor=executor,
        )
    ) as client:
        first = client.post(
            "/api/v1/chat/simple",
            json={"message": "Rank districts offering more than 200 minutes of planning time"},
        )
        session_id = first.json()["session"]["session_id"]
        second = client.post(
            "/api/v1/chat/simple",
            json={
                "message": levels[2],
                "session_id": session_id,
                "selected_option": levels[2],
            },
        )

    assert first.json()["turn"]["route"] == "clarify"
    assert second.status_code == 200
    # The guard fired: the planner ran a SECOND time (deterministic resume was
    # declined) rather than looping on the stale-filter clarify.
    assert len(planner.prompts) == 2
    assert second.json()["turn"]["route"] == "execute"
    # The re-planned filter is keyed to the resolved metric, not the stale phrase.
    assert executor.plans[1].filters[0].field == levels[2]


def test_fresh_chat_planner_direct_metric_clarify_grounds_and_resumes() -> None:
    """#1348 M1: the planner-direct half of the feature, end-to-end.

    The planner emits route=clarify with the single ambiguous phrase echoed into
    `candidates` and no options of its own; the grounding stage expands it
    through `executor.catalog.resolve_metric_bundle` into grounded options that
    persist to memory and resume on click. The unit tests cover the grounder in
    isolation; this exercises the executor.catalog seam + persistence + click
    round-trip that only `create_app` integration reaches.
    """
    from compass_backend.catalog import MetricBundleResolution, MetricCandidate

    # These three planning-time metrics are numeric (minutes). The catalog
    # derives answer_type='numeric' for them (db/catalog.py CASE: any numeric
    # answer value → 'numeric'); the fixture models that so #1830's rank-clarify
    # numeric filter keeps them (the 2 categorical policy siblings, which derive
    # 'text', are the ones that filter drops).
    levels = [
        MetricCandidate(
            metric_id=57,
            name="Minimum amount of elementary teacher planning time per week (in minutes)",
            answer_type="numeric",
            topic="Planning time",
        ),
        MetricCandidate(
            metric_id=58,
            name="Minimum amount of middle school teacher planning time per week (in minutes)",
            answer_type="numeric",
            topic="Planning time",
        ),
        MetricCandidate(
            metric_id=59,
            name="Minimum amount of high school teacher planning time per week (in minutes)",
            answer_type="numeric",
            topic="Planning time",
        ),
    ]

    class _MetricCatalog:
        async def resolve_metric_bundle(  # noqa: ANN001
            self, query, *, numeric_only=False, limit=5, degree_lane=None
        ):
            cands = levels if query.strip().casefold() == "planning time" else []
            return MetricBundleResolution(
                query=query, resolved=[], candidates=list(cands)
            )

    class _CatalogExecutor(SequenceQueryExecutor):
        def __init__(self, outcomes) -> None:  # noqa: ANN001
            super().__init__(outcomes)
            self.catalog = _MetricCatalog()

    clarify_turn = PlannerTurn(
        route="clarify",
        confidence=0.6,
        clarification=ClarificationRequest(
            question="Planning time is tracked across a few metrics — which to rank by?",
            missing_fields=["metric"],
            candidates=["planning time"],  # the single echoed phrase, no options
            pending_context=PendingQueryContext(
                operation="rank",
                selection=SelectionSpec(scope="all_covered_districts"),
                sort=SortSpec(field="planning time", direction="desc"),
                limit=LimitSpec(count=5, kind="top"),
                missing_fields=["metric"],
            ),
        ),
    )
    fallback = PlannerTurn(
        route="clarify",
        confidence=0.5,
        clarification=ClarificationRequest(
            question="Which metric?",
            missing_fields=["metric"],
            pending_context=PendingQueryContext(
                operation="rank",
                selection=SelectionSpec(scope="all_covered_districts"),
                missing_fields=["metric"],
            ),
        ),
    )
    planner = SequencePlannerAgent([clarify_turn, fallback])
    success = ExecutionSuccess(
        result=_ranking_result(),
        authority=ValidationAuthority(),
        message="Top 5 districts by planning time.",
    )
    executor = _CatalogExecutor([success])
    store = InMemorySessionStore()

    with TestClient(
        create_app(
            planner_agent=planner, session_store=store, query_executor=executor
        )
    ) as client:
        first = client.post(
            "/api/v1/chat/simple",
            json={"message": "Show me the 5 districts that offer the most planning time"},
        )
        body1 = first.json()
        session_id = body1["session"]["session_id"]
        second = client.post(
            "/api/v1/chat/simple",
            json={
                "message": levels[0].name,
                "session_id": session_id,
                "selected_option": levels[0].name,
            },
        )

    # Turn 1: the single echoed phrase was expanded into the three grounded,
    # canonical metric names — the planner-direct grounding seam fired.
    assert body1["turn"]["route"] == "clarify"
    opts = body1["turn"]["clarification"]["candidate_options"]
    assert [o["value"] for o in opts] == [c.name for c in levels]
    # Turn 2: clicking resumes deterministically to exactly that one metric.
    body2 = second.json()
    assert body2["turn"]["route"] == "execute"
    assert [m["name"] for m in body2["turn"]["query_plan"]["metrics"]] == [levels[0].name]
    assert len(planner.prompts) == 1  # the click skipped the planner


def test_fresh_chat_metric_click_preserves_resolved_sibling_metric() -> None:
    """#1348 m1: clicking a level in a multi-metric clarify keeps resolved siblings.

    "Compare salary AND planning time" resolves salary cleanly but planning time
    is ambiguous, so execution clarifies for that one phrase
    (``ambiguous_metric_phrase="planning time"``). The pending context must keep
    salary alongside the planning-time candidate levels; clicking a level must
    collapse ONLY the ambiguous set, leaving salary in the executed plan.
    """
    levels = [
        "Minimum amount of elementary teacher planning time per week (in minutes)",
        "Minimum amount of middle school teacher planning time per week (in minutes)",
        "Minimum amount of high school teacher planning time per week (in minutes)",
    ]
    sibling = "starting teacher salary"
    execute_turn = PlannerTurn(
        route="execute",
        confidence=0.8,
        query_plan=QueryPlan(
            operation="lookup",
            question="Compare starting salary and planning time across districts.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name=sibling), MetricSpec(name="planning time")],
        ),
    )
    fallback = PlannerTurn(
        route="clarify",
        confidence=0.5,
        clarification=ClarificationRequest(
            question="Which planning-time metric?",
            missing_fields=["metric"],
            pending_context=PendingQueryContext(
                operation="lookup",
                selection=SelectionSpec(scope="all_covered_districts"),
                missing_fields=["metric"],
            ),
        ),
    )
    planner = SequencePlannerAgent([execute_turn, fallback])
    clarification = ExecutionClarification(
        clarification=ClarificationRequest(
            question='I found a few Compass metrics that could match "planning time".',
            missing_fields=["metric"],
            candidates=levels,
            candidate_options=[
                ClarificationOption(value=name, label=name) for name in levels
            ],
        ),
        message='I found a few Compass metrics that could match "planning time".',
        # m1: execution tells the builder which phrase was ambiguous so the
        # already-resolved sibling (salary) is preserved, not dropped.
        ambiguous_metric_phrase="planning time",
    )
    success = ExecutionSuccess(
        result=_ranking_result(),
        authority=ValidationAuthority(),
        message="Compared salary and middle-school planning time.",
    )
    executor = SequenceQueryExecutor([clarification, success])
    store = InMemorySessionStore()

    with TestClient(
        create_app(
            planner_agent=planner, session_store=store, query_executor=executor
        )
    ) as client:
        first = client.post(
            "/api/v1/chat/simple",
            json={"message": "Compare starting salary and planning time across districts."},
        )
        session_id = first.json()["session"]["session_id"]
        # The persisted pending context keeps salary alongside the level candidates.
        snapshots = anyio.run(store.snapshots_for_session, session_id)
        assert [m.name for m in snapshots[0].pending_query_context.metrics] == [
            sibling,
            *levels,
        ]
        second = client.post(
            "/api/v1/chat/simple",
            json={
                "message": levels[1],
                "session_id": session_id,
                "selected_option": levels[1],
            },
        )

    plan = second.json()["turn"]["query_plan"]
    # Sibling preserved; ambiguous slot collapsed to exactly the clicked level.
    assert [m["name"] for m in plan["metrics"]] == [sibling, levels[1]]
    assert executor.plans[1].metrics == [
        MetricSpec(name=sibling),
        MetricSpec(name=levels[1]),
    ]


def test_fresh_chat_compound_metric_district_click_defers_to_planner() -> None:
    """#1348 m3: a compound clarify defers to the planner on click.

    The resume dispatch keys on an EXACT single-dimension match
    (``missing_fields == ["metric"]`` / ``== ["district"]``); a compound
    ``["metric","district"]`` hits the else-branch and returns False so the
    planner re-resolves. This pins that contract against a future ``==`` → ``in``
    refactor that would silently half-resume a compound clarify.
    """
    execute_turn = PlannerTurn(
        route="execute",
        confidence=0.8,
        query_plan=QueryPlan(
            operation="rank",
            question="Top districts by planning time.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="planning time")],
            sort=SortSpec(field="planning time", direction="desc"),
        ),
    )
    replan = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="rank",
            question="Top districts by planning time.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="elementary planning time")],
            sort=SortSpec(field="elementary planning time", direction="desc"),
        ),
    )
    planner = SequencePlannerAgent([execute_turn, replan])
    clarification = ExecutionClarification(
        clarification=ClarificationRequest(
            question="Which metric, and which district?",
            missing_fields=["metric", "district"],
            candidates=["elementary planning time", "middle planning time"],
            candidate_options=[
                ClarificationOption(value="elementary planning time", label="Elementary"),
                ClarificationOption(value="middle planning time", label="Middle"),
            ],
        ),
        message="Which metric, and which district?",
    )
    success = ExecutionSuccess(
        result=_ranking_result(),
        authority=ValidationAuthority(),
        message="Ranked.",
    )
    executor = SequenceQueryExecutor([clarification, success])
    store = InMemorySessionStore()

    with TestClient(
        create_app(
            planner_agent=planner, session_store=store, query_executor=executor
        )
    ) as client:
        first = client.post(
            "/api/v1/chat/simple",
            json={"message": "Top districts by planning time."},
        )
        session_id = first.json()["session"]["session_id"]
        second = client.post(
            "/api/v1/chat/simple",
            json={
                "message": "elementary planning time",
                "session_id": session_id,
                "selected_option": "elementary planning time",
            },
        )

    assert first.json()["turn"]["clarification"]["missing_fields"] == ["metric", "district"]
    # The compound clarify deferred to the planner (it ran a SECOND time) rather
    # than unilaterally filling the metric slot from the click.
    assert len(planner.prompts) == 2
    assert second.json()["turn"]["route"] == "execute"


def test_fresh_chat_metric_grounding_catalog_failure_degrades_to_prose() -> None:
    """#1348 T3: a grounding-adjudicator failure leaves a prose clarify, not a 500.

    The planner-direct metric grounder calls resolve_metric_bundle, which can run
    the catalog adjudicator LLM. If it raises, the clarify turn must still return
    200 as a prose clarify (no options) rather than 500-ing an answerable turn.
    """

    class _RaisingMetricCatalog:
        async def resolve_metric_bundle(  # noqa: ANN001
            self, query, *, numeric_only=False, limit=5, degree_lane=None
        ):
            raise RuntimeError("adjudicator boom")

    class _CatalogExecutor(SequenceQueryExecutor):
        def __init__(self, outcomes) -> None:  # noqa: ANN001
            super().__init__(outcomes)
            self.catalog = _RaisingMetricCatalog()

    clarify_turn = PlannerTurn(
        route="clarify",
        confidence=0.6,
        clarification=ClarificationRequest(
            question="Planning time breaks down by level — which would you like?",
            missing_fields=["metric"],
            candidates=["planning time"],
        ),
    )
    planner = SequencePlannerAgent([clarify_turn])
    executor = _CatalogExecutor([])
    store = InMemorySessionStore()

    with TestClient(
        create_app(
            planner_agent=planner, session_store=store, query_executor=executor
        )
    ) as client:
        resp = client.post(
            "/api/v1/chat/simple",
            json={"message": "Show me the 5 districts that offer the most planning time"},
        )

    # The grounding failure degraded to the planner's prose clarify — no 500.
    assert resp.status_code == 200
    turn = resp.json()["turn"]
    assert turn["route"] == "clarify"
    assert turn["clarification"]["candidate_options"] == []


def test_fresh_chat_routes_health_coverage_prompt_to_policy_guidance() -> None:
    # "Show me districts where teachers get great health coverage." is a
    # subjective benefits-exemplar ask. Per #1088 we route it at the planner:
    # the health-benefit-exemplar snippet (see test_planner_shape_check
    # ::test_health_benefit_exemplar_fires_for_great_health_coverage) guides the
    # planner to emit route="policy_guidance" directly, rather than routing to
    # execute and relying on a below-planner execution recovery (retired in
    # M1 #1006). End-to-end routing with the real planner is covered by B-spine
    # case FEEDBACK-122-BENEFITS-EXEMPLARS (case_id 1124). This test verifies
    # the policy_guidance render path: benefits exemplars, no metric clarify.
    planner = SequencePlannerAgent(
        [
            PlannerTurn(
                route="policy_guidance",
                confidence=0.72,
                policy_guidance=PolicyGuidancePlan(
                    topic_ids=["benefits"],
                    layers=["exemplars"],
                    intent_summary="User asked for districts with great health coverage.",
                    focus_terms=["health benefits"],
                ),
            )
        ]
    )
    store = InMemorySessionStore()
    executor = SequenceQueryExecutor([])

    reset_library()
    load_default_library()
    try:
        with TestClient(
            create_app(
                planner_agent=planner,
                session_store=store,
                query_executor=executor,
            )
        ) as client:
            response = client.post(
                "/api/v1/chat/simple",
                json={
                    "message": (
                        "Show me districts where teachers get great health coverage."
                    )
                },
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["turn"]["route"] == "policy_guidance"
        assert body["turn"]["policy_guidance"]["topic_ids"] == ["benefits"]
        assert body["turn"]["policy_guidance"]["layers"] == ["exemplars"]
        assert body["turn"]["policy_guidance"]["focus_terms"] == ["health benefits"]
        assert "Broward County Public Schools" in body["message"]
        assert "Wichita Public Schools (KS)" in body["message"]
        assert "Jackson Public Schools (MS)" in body["message"]
        assert "I found a few Compass metrics" not in body["message"]
        assert executor.plans == []
    finally:
        reset_library()


def test_planner_output_validator_requires_pending_context_on_clarify() -> None:
    turn = PlannerTurn(
        route="clarify",
        confidence=0.72,
        clarification=ClarificationRequest(
            question="Which salary metric should I use?",
            missing_fields=["metric"],
        ),
    )

    with pytest.raises(ModelRetry, match="pending_context"):
        validate_planner_turn_quality(
            turn,
            PlannerDeps(message="how much does california pay teachers?"),
        )


def test_planner_output_validator_rejects_dropped_pending_slots() -> None:
    turn = PlannerTurn(
        route="clarify",
        confidence=0.72,
        clarification=ClarificationRequest(
            question="Which districts should I use?",
            missing_fields=["district"],
            pending_context=PendingQueryContext(
                missing_fields=["district"],
            ),
        ),
    )

    with pytest.raises(ModelRetry, match="dropped prior typed slots"):
        validate_planner_turn_quality(
            turn,
            PlannerDeps(
                message="salary",
                memory=ConversationMemory(
                    pending_query_context=PendingQueryContext(
                        operation="lookup",
                        metrics=[MetricSpec(name="starting salary")],
                        missing_fields=["district"],
                    ),
                ),
            ),
        )


def test_planner_output_validator_rejects_partial_committed_metric_drop() -> None:
    prior_metrics = [
        MetricSpec(name="Does the district cover 100% of employee premium?"),
        MetricSpec(name="Employee health insurance premium contribution"),
        MetricSpec(name="Dependent health insurance premium contribution"),
        MetricSpec(name="Sick leave days"),
    ]
    turn = PlannerTurn(
        route="clarify",
        confidence=0.72,
        clarification=ClarificationRequest(
            question="Which districts should I use?",
            missing_fields=["district", "scope"],
            pending_context=PendingQueryContext(
                operation="rank",
                metrics=[prior_metrics[0]],
                missing_fields=["district", "scope"],
            ),
        ),
    )

    with pytest.raises(ModelRetry, match="dropped prior typed slots: metrics"):
        validate_planner_turn_quality(
            turn,
            PlannerDeps(
                message="do all 4 separately",
                memory=ConversationMemory(
                    pending_query_context=PendingQueryContext(
                        operation="rank",
                        metrics=prior_metrics,
                        missing_fields=["district", "scope"],
                    ),
                ),
            ),
        )


def test_planner_output_validator_allows_metric_choice_when_no_metric_committed() -> None:
    selected_metric = MetricSpec(name="Employee health insurance premium contribution")
    turn = PlannerTurn(
        route="clarify",
        confidence=0.72,
        clarification=ClarificationRequest(
            question="Which districts should I use?",
            missing_fields=["district", "scope"],
            pending_context=PendingQueryContext(
                operation="rank",
                metrics=[selected_metric],
                missing_fields=["district", "scope"],
            ),
        ),
    )

    validated = validate_planner_turn_quality(
        turn,
        PlannerDeps(
            message="use the employee premium one",
            memory=ConversationMemory(
                pending_query_context=PendingQueryContext(
                    operation="rank",
                    missing_fields=["metric"],
                ),
            ),
        ),
    )

    assert validated is turn


def test_pending_context_after_turn_preserves_committed_multi_metric_slots() -> None:
    from compass_backend.planning.planner import pending_context_after_turn

    prior_metrics = [
        MetricSpec(name="Does the district cover 100% of employee premium?"),
        MetricSpec(name="Employee health insurance premium contribution"),
        MetricSpec(name="Dependent health insurance premium contribution"),
        MetricSpec(name="Sick leave days"),
    ]
    turn = PlannerTurn(
        route="clarify",
        confidence=0.72,
        clarification=ClarificationRequest(
            question="I can show one at a time. Start with the first?",
            missing_fields=["district", "scope"],
            pending_context=PendingQueryContext(
                operation="rank",
                metrics=[prior_metrics[0]],
                missing_fields=["district", "scope"],
            ),
        ),
    )

    merged = pending_context_after_turn(
        turn,
        PendingQueryContext(
            operation="rank",
            metrics=prior_metrics,
            missing_fields=["district", "scope"],
        ),
        turn_index=2,
    )

    assert merged is not None
    assert merged.metrics == prior_metrics


def test_planner_output_validator_allows_complete_pending_for_runtime_promotion() -> None:
    turn = PlannerTurn(
        route="clarify",
        confidence=0.72,
        clarification=ClarificationRequest(
            question="Which districts should I use?",
            missing_fields=["district"],
            pending_context=PendingQueryContext(
                operation="lookup",
                selection=SelectionSpec(scope="state", states=["CA"]),
                metrics=[MetricSpec(name="starting salary")],
                missing_fields=[],
            ),
        ),
    )

    validated = validate_planner_turn_quality(
        turn,
        PlannerDeps(
            message="salary",
            memory=ConversationMemory(
                pending_query_context=PendingQueryContext(
                    selection=SelectionSpec(scope="state", states=["CA"]),
                    missing_fields=["metric"],
                ),
            ),
        ),
    )

    assert validated is turn


def test_planner_output_validator_keeps_policy_guidance_detail_clarification() -> None:
    turn = PlannerTurn(
        route="clarify",
        confidence=0.85,
        clarification=ClarificationRequest(
            question='I found a few Compass metrics that could match "performance pay".',
            missing_fields=["metric"],
            pending_context=PendingQueryContext(
                operation="lookup",
                selection=SelectionSpec(
                    scope="named_districts",
                    districts=["District of Columbia Public Schools"],
                ),
                metrics=[
                    MetricSpec(name="performance pay"),
                    MetricSpec(name="differentiated pay"),
                ],
                missing_fields=["metric"],
            ),
        ),
    )

    validated = validate_planner_turn_quality(
        turn,
        PlannerDeps(
            message="Now show me the actual policy details for the top one.",
            memory=ConversationMemory(recent_routes=["policy_guidance"]),
        ),
    )

    assert validated is turn


def test_planner_output_validator_keeps_frpl_salary_display_clarification() -> None:
    turn = PlannerTurn(
        route="clarify",
        confidence=0.82,
        clarification=ClarificationRequest(
            question='I found a few Compass metrics that could match "starting teacher salary".',
            candidates=[
                "Annual base salary for a first year teacher with a bachelor's degree",
                "Annual base salary for a first year teacher with a master's degree",
            ],
            missing_fields=["metric"],
            pending_context=PendingQueryContext(
                operation="lookup",
                selection=SelectionSpec(scope="all_covered_districts"),
                metrics=[
                    MetricSpec(name="starting teacher salary"),
                    MetricSpec(name="free-and-reduced lunch share", role="comparison"),
                ],
                missing_fields=["metric"],
            ),
        ),
    )

    validated = validate_planner_turn_quality(
        turn,
        PlannerDeps(
            message=(
                "Show me starting teacher salaries for districts with the "
                "highest free-and-reduced lunch share."
            ),
        ),
    )

    assert validated is turn


def test_planner_output_validator_keeps_frpl_salary_scope_clarification() -> None:
    turn = PlannerTurn(
        route="clarify",
        confidence=0.82,
        clarification=ClarificationRequest(
            question=(
                "How would you like to scope this? Do you mean all covered "
                "districts ranked by highest free-and-reduced lunch share, "
                "a specific state, or a top N?"
            ),
            candidates=[
                "All covered districts, ranked by highest FRL share",
                "A specific state",
                "Top N districts by FRL share",
            ],
            missing_fields=["scope", "output_format"],
            pending_context=PendingQueryContext(
                operation="lookup",
                selection=SelectionSpec(scope="all_covered_districts"),
                metrics=[
                    MetricSpec(name="starting teacher salary"),
                    MetricSpec(name="free-and-reduced lunch share", role="comparison"),
                ],
                missing_fields=["scope", "output_format"],
            ),
        ),
    )

    validated = validate_planner_turn_quality(
        turn,
        PlannerDeps(
            message=(
                "Show me starting teacher salaries for districts with the "
                "highest free-and-reduced lunch share."
            ),
        ),
    )

    assert validated is turn


def test_planner_output_validator_keeps_peer_max_salary_clarification() -> None:
    turn = PlannerTurn(
        route="clarify",
        confidence=0.9,
        clarification=ClarificationRequest(
            question='I found a few Compass metrics that could match "maximum teacher salary".',
            candidates=[
                "Maximum base salary for a teacher with a bachelor's degree",
                "Maximum base salary for a teacher with a master's degree",
            ],
            missing_fields=["metric"],
            pending_context=PendingQueryContext(
                operation="peer_comparison",
                selection=SelectionSpec(
                    scope="named_districts",
                    districts=["San Bernardino City Unified"],
                ),
                metrics=[MetricSpec(name="maximum teacher salary")],
                missing_fields=["metric"],
            ),
        ),
    )

    validated = validate_planner_turn_quality(
        turn,
        PlannerDeps(
            message=(
                "What is the maximum teacher salary in San Bernardino "
                "City Unified and comparable districts?"
            ),
        ),
    )

    assert validated is turn


def test_planner_output_validator_rejects_inheritance_without_query_context() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.8,
        query_plan=QueryPlan(
            inherit_from_session=True,
            question="Sort those highest first.",
            selection=SelectionSpec(scope="unspecified"),
            metrics=[],
            sort=SortSpec(field="<inherit>", direction="desc"),
        ),
    )

    with pytest.raises(ModelRetry, match="requires previous validated query context"):
        validate_planner_turn_quality(
            turn,
            PlannerDeps(message="Sort those highest first."),
        )


def test_planner_output_validator_rejects_prior_result_rows_without_rows() -> None:
    previous = QueryPlan(
        question="How many districts offer performance pay?",
        operation="count",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="performance pay")],
    )
    turn = PlannerTurn(
        route="execute",
        confidence=0.8,
        query_plan=QueryPlan(
            inherit_from_session=True,
            inherit_selection_from="prior_result_rows",
            question="Sort those highest first.",
            selection=SelectionSpec(scope="unspecified"),
            metrics=[],
            sort=SortSpec(field="<inherit>", direction="desc"),
        ),
    )

    with pytest.raises(ModelRetry, match="requires previous validated result rows"):
        validate_planner_turn_quality(
            turn,
            PlannerDeps(
                message="Sort those highest first.",
                memory=ConversationMemory(
                    latest_query_context=QueryContext(
                        query_plan=previous,
                        result_type="metric_count",
                    ),
                ),
            ),
        )


def test_fresh_chat_direct_turn_preserves_pending_context() -> None:
    planner = SequencePlannerAgent(
        [
            PlannerTurn(
                route="clarify",
                confidence=0.75,
                clarification=ClarificationRequest(
                    question="Which district should I use?",
                    missing_fields=["district"],
                    pending_context=PendingQueryContext(
                        operation="lookup",
                        metrics=[MetricSpec(name="starting salary")],
                        missing_fields=["district"],
                    ),
                ),
            ),
            _direct_turn("I can still help with that."),
        ]
    )
    store = InMemorySessionStore()

    with TestClient(create_app(planner_agent=planner, session_store=store)) as client:
        first_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "starting salary"},
        )
        session_id = first_response.json()["session"]["session_id"]
        second_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "thanks", "session_id": session_id},
        )

    assert second_response.status_code == 200
    reloaded = anyio.run(store.load, session_id)
    assert reloaded.pending_query_context is not None
    assert reloaded.pending_query_context.metrics == [
        MetricSpec(name="starting salary")
    ]


def test_fresh_chat_stores_planner_run_evidence_without_public_envelope_change() -> None:
    store = InMemorySessionStore()
    turn = _direct_turn("Evidence response.")
    agent = _agent_for_turn(turn)

    with TestClient(create_app(planner_agent=agent, session_store=store)) as client:
        response = client.post("/api/v1/chat/simple", json={"message": "hello"})

    body = response.json()
    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])
    assert "planner_evidence" not in body
    assert "planner_evidence" not in body["session"]
    assert snapshots[0].planner_evidence is not None
    assert snapshots[0].planner_evidence.new_messages_json

    from pydantic_ai.messages import ModelMessagesTypeAdapter

    decoded = ModelMessagesTypeAdapter.validate_json(
        snapshots[0].planner_evidence.new_messages_json
    )
    assert decoded


def test_run_planner_persists_selected_guidance_in_evidence() -> None:
    agent = _agent_for_turn(_direct_turn("Guided."))

    async def run_it():
        return await run_planner(
            "Show the five highest starting salaries.",
            model="test-model",
            agent=agent,
            trace_id="trace-guidance",
        )

    run = anyio.run(run_it)

    assert run.evidence is not None
    assert [item.name for item in run.evidence.planner_guidance] == [
        "ranking-and-sorting",
        "teacher-compensation-salary",
    ]
    assert run.evidence.planner_guidance[0].metadata == {}
    assert run.evidence.trace_id == "trace-guidance"


def test_run_planner_persists_policy_guidance_followup_guidance() -> None:
    agent = _agent_for_turn(_direct_turn("Guided."))
    memory = ConversationMemory(recent_routes=["policy_guidance"])

    async def run_it():
        return await run_planner(
            "Now show me the actual policy details for the top one.",
            model="test-model",
            agent=agent,
            memory=memory,
            trace_id="trace-policy-guidance-followup",
        )

    run = anyio.run(run_it)

    assert run.evidence is not None
    assert [item.name for item in run.evidence.planner_guidance] == [
        "policy-guidance-followups"
    ]
    assert run.evidence.planner_guidance[0].matched_phrase == "details"
    assert run.evidence.trace_id == "trace-policy-guidance-followup"


def test_fresh_chat_hides_planner_guidance_evidence_from_public_response() -> None:
    store = InMemorySessionStore()
    agent = _agent_for_turn(_direct_turn("Guided response."))

    with TestClient(
        create_app(
            planner_agent=agent,
            session_store=store,
            app_settings=Settings(
                session_store_backend="memory",
            ),
        )
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={"message": "Show the five highest starting salaries."},
        )

    body = response.json()
    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])
    assert "planner_guidance" not in body
    assert "planner_guidance" not in body["session"]
    assert snapshots[0].planner_evidence is not None
    assert [item.name for item in snapshots[0].planner_evidence.planner_guidance] == [
        "ranking-and-sorting",
        "teacher-compensation-salary",
    ]


def test_fresh_chat_hides_planner_thinking_evidence_from_public_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        planner_module.agent_model_settings,
        "planner_thinking_policy",
        "all",
    )
    monkeypatch.setattr(
        planner_module.agent_model_settings,
        "planner_thinking_max_effort",
        "low",
    )
    store = InMemorySessionStore()
    turn = _direct_turn("Thinking evidence stays private.")
    agent = _agent_for_turn(turn)

    with TestClient(create_app(planner_agent=agent, session_store=store)) as client:
        response = client.post("/api/v1/chat/simple", json={"message": "hello"})

    body = response.json()
    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])
    assert "thinking_enabled" not in body
    assert "thinking_effort" not in body
    assert "planner_evidence" not in body
    assert "planner_evidence" not in body["session"]
    assert snapshots[0].planner_evidence is not None
    assert snapshots[0].planner_evidence.thinking_enabled is True
    assert snapshots[0].planner_evidence.thinking_effort == "low"


def test_fresh_chat_threads_conversation_memory_into_followup_planner_deps() -> None:
    store = InMemorySessionStore()
    first_plan = QueryPlan(
        question="Rank all districts by starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format="table"),
    )
    planner = SequencePlannerAgent(
        [
            PlannerTurn(route="execute", confidence=0.91, query_plan=first_plan),
            _direct_turn("I can chart the previous result."),
        ]
    )
    executor = SequenceQueryExecutor(
        [
            ExecutionSuccess(
                result=_ranking_result(),
                authority=ValidationAuthority(),
                message="Ranked by starting salary, highest to lowest.",
            )
        ]
    )

    with TestClient(
        create_app(
            planner_agent=planner,
            session_store=store,
            query_executor=executor,
        )
    ) as client:
        first_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "Rank all districts by starting salary."},
        )
        session_id = first_response.json()["session"]["session_id"]
        second_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "Let's try that one as a chart.", "session_id": session_id},
        )

    assert second_response.status_code == 200
    assert "conversation_memory" not in second_response.json()["session"]
    assert "result_memory_refs" not in second_response.json()["session"]
    followup_deps = planner.kwargs[1]["deps"]
    assert followup_deps.memory.summary is not None
    assert followup_deps.memory.summary.active_user_goal == (
        "Rank all districts by starting salary."
    )
    assert len(followup_deps.result_memory_refs) == 1
    assert followup_deps.result_memory_refs[0].question == (
        "Rank all districts by starting salary."
    )
    snapshots = anyio.run(store.snapshots_for_session, session_id)
    assert snapshots[0].result is not None
    assert snapshots[0].result_memory_refs[0].has_csv_export is True


def test_oversized_snapshot_result_is_skipped_but_result_ref_is_retained() -> None:
    store = InMemorySessionStore()
    plan = QueryPlan(
        question="Rank all districts by starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format="table"),
    )
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_ranking_result(),
            authority=ValidationAuthority(),
            message="Ranked by starting salary, highest to lowest.",
        )
    )

    with TestClient(
        create_app(
            planner_agent=_agent_for_turn(
                PlannerTurn(route="execute", confidence=0.91, query_plan=plan)
            ),
            session_store=store,
            query_executor=executor,
            app_settings=Settings(
                session_store_backend="memory",
                conversation_memory_result_max_bytes=1,
            ),
        )
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={"message": "Rank all districts by starting salary."},
        )

    assert response.status_code == 200
    body = response.json()
    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])

    assert body["result"] is not None
    assert snapshots[0].result is None
    assert snapshots[0].result_memory_refs[0].question == (
        "Rank all districts by starting salary."
    )


def test_query_plan_does_not_accept_planner_evidence_as_execution_authority() -> None:
    evidence = PlannerRunEvidence(
        new_messages_json="[]",
        message_count=0,
        model="test-model",
    )

    with pytest.raises(ValidationError):
        QueryPlan(
            question="unsafe",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting salary")],
            planner_evidence=evidence,
        )


def test_query_plan_does_not_accept_planning_brief_as_execution_authority() -> None:
    with pytest.raises(ValidationError):
        QueryPlan(
            question="unsafe",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting salary")],
            planning_brief={"candidate_operation": "count"},
        )


def test_fresh_chat_mvp_rejects_long_messages() -> None:
    agent = _agent_for_turn(_direct_turn("unused"))

    with TestClient(create_app(planner_agent=agent)) as client:
        response = client.post("/api/v1/chat/simple", json={"message": "x" * 2001})

    assert response.status_code == 422
    assert "Message exceeds 2000 characters" in response.text


def test_create_app_from_settings_passes_settings_to_chat_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The chat-pool startup hook would otherwise try to reach localhost
    # Postgres. This test only exercises the routing wire-up; patch the
    # pool factory to a no-op so the lifespan completes.
    async def _no_pool(_source: object) -> None:
        return None

    monkeypatch.setattr("compass_backend.api.app.create_chat_pool", _no_pool)

    app = create_app_from_settings(Settings(chat_message_max_chars=3))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/v1/chat/simple", json={"message": "four"})

    assert response.status_code == 422
    assert "Message exceeds 3 characters" in response.text


def test_default_chat_router_reuses_one_cached_planner_agent(monkeypatch) -> None:
    planner = SequencePlannerAgent(
        [
            _direct_turn("First cached planner response."),
            _direct_turn("Second cached planner response."),
        ]
    )
    created_models: list[str] = []

    def fake_create_planner_agent(model: str, **_kwargs) -> SequencePlannerAgent:
        created_models.append(model)
        return planner

    monkeypatch.setattr(
        planner_module,
        "create_planner_agent",
        fake_create_planner_agent,
    )

    reset_library()
    app = create_app(
        session_store=InMemorySessionStore(),
        app_settings=Settings(session_store_backend="memory"),
    )
    set_library(_toy_policy_guidance_library())

    try:
        with TestClient(app) as client:
            first_response = client.post(
                "/api/v1/chat/simple",
                json={"message": "hello"},
            )
            second_response = client.post(
                "/api/v1/chat/simple",
                json={"message": "hello again"},
            )
    finally:
        reset_library()

    assert first_response.status_code == 200
    assert first_response.json()["message"] == "First cached planner response."
    assert second_response.status_code == 200
    assert second_response.json()["message"] == "Second cached planner response."
    assert created_models == [agent_model_settings.planner_model]
    assert planner.prompts == ["hello", "hello again"]


def test_default_chat_router_lazily_binds_loaded_policy_guidance_library(
    monkeypatch,
) -> None:
    planner = SequencePlannerAgent([_direct_turn("Bound planner response.")])
    library = _toy_policy_guidance_library()
    created_libraries: list[PolicyGuidanceLibrary | None] = []

    def fake_create_planner_agent(
        model: str,
        *,
        library: PolicyGuidanceLibrary | None = None,
    ) -> SequencePlannerAgent:
        del model
        created_libraries.append(library)
        assert library is not None
        Bound = planner_module.make_bound_planner_turn(library)
        schema = Bound.model_json_schema()
        plan_schema = schema["$defs"]["BoundPolicyGuidancePlan"]
        assert sorted(plan_schema["properties"]["topic_ids"]["items"]["enum"]) == [
            "runtime-topic",
            "second-runtime-topic",
        ]
        return planner

    monkeypatch.setattr(
        planner_module,
        "create_planner_agent",
        fake_create_planner_agent,
    )
    reset_library()
    app = create_app(
        session_store=InMemorySessionStore(),
        app_settings=Settings(session_store_backend="memory"),
    )
    set_library(library)

    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/chat/simple",
                json={"message": "hello"},
            )
    finally:
        reset_library()

    assert response.status_code == 200
    assert response.json()["message"] == "Bound planner response."
    assert created_libraries == [library]


def test_policy_guidance_chat_gates_validation_failed_manifest(monkeypatch) -> None:
    turn = PlannerTurn(
        route="policy_guidance",
        confidence=0.97,
        policy_guidance=PolicyGuidancePlan(
            topic_ids=["runtime-topic"],
            layers=["stances"],
            intent_summary="Planner-authored prose must not leak.",
        ),
    )

    def fake_render_policy_guidance(*_args, **_kwargs) -> ResponseManifest:
        return ResponseManifest(
            body="Invalid renderer body with raw source marker [999].",
            status="validation_failed",
            result_type="policy_guidance",
            validation_valid=False,
            warnings=["metadata_citation_not_in_body:marker=[999]"],
            metadata={"citations": []},
        )

    monkeypatch.setattr(
        chat_module,
        "render_policy_guidance",
        fake_render_policy_guidance,
    )
    reset_library()
    set_library(_toy_policy_guidance_library())

    try:
        with TestClient(
            create_app(
                planner_agent=SequencePlannerAgent([turn]),
                session_store=InMemorySessionStore(),
            )
        ) as client:
            response = client.post(
                "/api/v1/chat/simple",
                json={"message": "show policy guidance"},
            )
    finally:
        reset_library()

    assert response.status_code == 200
    assert response.json()["message"] == POLICY_GUIDANCE_VALIDATION_FAILED_BODY


def test_create_app_uses_configured_memory_session_store() -> None:
    agent = _agent_for_turn(_direct_turn("Configured memory response."))

    with TestClient(
        create_app(
            planner_agent=agent,
            app_settings=Settings(session_store_backend="memory"),
        )
    ) as client:
        response = client.post("/api/v1/chat/simple", json={"message": "hello"})

    assert response.status_code == 200
    assert response.json()["message"] == "Configured memory response."


def test_fresh_chat_mvp_continues_existing_session() -> None:
    store = InMemorySessionStore()
    agent = _agent_for_turn(_direct_turn("Session response."))

    with TestClient(create_app(planner_agent=agent, session_store=store)) as client:
        first_response = client.post("/api/v1/chat/simple", json={"message": "hello"})
        session_id = first_response.json()["session"]["session_id"]

        second_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "hello again", "session_id": session_id},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    second_body = second_response.json()
    assert second_body["session"]["session_id"] == session_id
    assert second_body["session"]["turn_count"] == 2
    assert second_body["session"]["latest_snapshot_id"] == second_body["snapshot_id"]


def test_fresh_chat_inherits_query_referents_for_sort_followup() -> None:
    store = InMemorySessionStore()
    first_plan = QueryPlan(
        question="Show California starting salaries.",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format="table"),
    )
    followup_turn = PlannerTurn(
        route="execute",
        confidence=0.84,
        query_plan=QueryPlan(
            inherit_from_session=True,
            question="Sort that table highest to lowest.",
            selection=SelectionSpec(scope="unspecified"),
            metrics=[],
            sort=SortSpec(field="value", direction="desc"),
        ),
    )
    planner = SequencePlannerAgent(
        [
            PlannerTurn(
                route="execute",
                confidence=0.9,
                query_plan=first_plan,
            ),
            followup_turn,
        ]
    )
    executor = SequenceQueryExecutor(
        [
            ExecutionSuccess(
                result=_ranking_result(),
                authority=ValidationAuthority(),
                message="Ranked California salaries.",
            ),
            ExecutionSuccess(
                result=_ranking_result(),
                authority=ValidationAuthority(),
                message="Sorted California salaries descending.",
            ),
        ]
    )

    with TestClient(
        create_app(
            planner_agent=planner,
            query_executor=executor,
            session_store=store,
        )
    ) as client:
        first_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "Show California starting salaries."},
        )
        session_id = first_response.json()["session"]["session_id"]
        second_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "Sort descending", "session_id": session_id},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    merged_followup = executor.plans[1]
    assert merged_followup.selection == first_plan.selection
    assert merged_followup.metrics == first_plan.metrics
    assert merged_followup.output == first_plan.output
    assert merged_followup.sort == SortSpec(field="value", direction="desc")
    second_body = second_response.json()
    assert second_body["turn"]["query_plan"]["selection"] == {
        "scope": "state",
        "districts": [],
        "states": ["CA"],
    }
    assert second_body["turn"]["query_plan"]["metrics"] == [
        {"name": "starting salary", "role": "primary", "degree_lane": None}
    ]
    assert second_body["turn"]["query_plan"]["output"] == {
        "format": "table",
        "include_citations": True,
        "row_display": "preview",
        "group_by": "none",
    }


def test_fresh_chat_gives_planner_prior_context_for_followup() -> None:
    store = InMemorySessionStore()
    first_plan = QueryPlan(
        question="Show the five highest starting salaries.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        sort=SortSpec(field="starting salary", direction="desc"),
        limit={"count": 5, "kind": "top"},
        output=OutputSpec(format="table"),
    )
    planner = SequencePlannerAgent(
        [
            PlannerTurn(
                route="execute",
                confidence=0.9,
                query_plan=first_plan,
            ),
            PlannerTurn(
                route="clarify",
                confidence=0.75,
                clarification=ClarificationRequest(
                    question="Should I compare bachelor's and master's lanes for the prior districts?",
                    missing_fields=["metric"],
                ),
            ),
        ]
    )
    executor = SequenceQueryExecutor(
        [
            ExecutionSuccess(
                result=_ranking_result(),
                authority=ValidationAuthority(),
                message="Ranked starting salaries.",
            ),
        ]
    )

    with TestClient(
        create_app(
            planner_agent=planner,
            query_executor=executor,
            session_store=store,
        )
    ) as client:
        first_response = client.post(
            "/api/v1/chat/simple",
            json={"message": "Show the five highest starting salaries."},
        )
        session_id = first_response.json()["session"]["session_id"]
        second_response = client.post(
            "/api/v1/chat/simple",
            json={
                "message": "how does this break down for bachelor's vs masters",
                "session_id": session_id,
            },
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert planner.prompts[0] == "Show the five highest starting salaries."
    assert planner.prompts[1] == "how does this break down for bachelor's vs masters"
    followup_deps = planner.kwargs[1]["deps"]
    assert followup_deps.query_context is not None
    assert followup_deps.query_context.query_plan.metrics == [
        MetricSpec(name="starting salary")
    ]
    assert followup_deps.query_context.result_districts[0].district_name == "Bravo"
    rendered_context = planner_context_instructions(followup_deps)
    assert "Non-authoritative Compass conversation memory" in rendered_context
    assert "starting salary" in rendered_context
    assert "Bravo" in rendered_context


def test_query_context_records_qualifying_districts_from_count_results() -> None:
    plan = QueryPlan(
        operation="count",
        question="How many districts observe non-tenured teachers more than twice?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="informal observations")],
    )
    result = MetricCountResult(
        selection=ResultSelection(
            scope="all_covered_districts",
            districts=[
                SelectedDistrict(district_id=10, district_name="Alpha", state="CA"),
                SelectedDistrict(district_id=11, district_name="Bravo", state="CA"),
                SelectedDistrict(district_id=12, district_name="Charlie", state="CA"),
            ],
        ),
        rows=[
            ThresholdCountRow(
                metric_id=39,
                metric_name="Informal observations",
                value=2,
                display_value="2 districts",
                academic_year="2024 - 2025",
                count=2,
                denominator=3,
                filter_statement="informal observations greater than 2",
                qualifying_district_ids=[10, 11],
                source="policy_answer",
                coverage_state="covered",
                coverage_display="2 districts",
                coverage_reason="threshold_count",
            )
        ],
        total_considered=3,
        excluded_count=1,
        coverage_frame=CoverageFrame(
            universe_count=3,
            in_scope_count=3,
            addressed_count=3,
            real_data_count=2,
            not_reviewed_count=0,
            out_of_universe_count=0,
            coverage_ratio=2 / 3,
        ),
        order_statement="Counted qualifying districts for selected metrics.",
        source_notes=[],
        methodology_codes=[
            MethodologyRef(code="count_denominator_current_reviewed_rows"),
        ],
    )

    context = chat_module._query_context_for_result(
        query_plan=plan,
        authority=ValidationAuthority(),
        result=result,
    )

    assert [
        (district.district_id, district.district_name)
        for district in context.result_districts
    ] == [(10, "Alpha"), (11, "Bravo")]


def test_merge_query_plan_replaces_inherit_sort_field_with_prior_metric() -> None:
    previous = QueryPlan(
        question="Show California starting salaries.",
        selection=SelectionSpec(scope="state", states=["CA"]),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format="table"),
    )
    followup = QueryPlan(
        inherit_from_session=True,
        question="Sort those highest to lowest.",
        selection=SelectionSpec(scope="unspecified"),
        metrics=[],
        sort=SortSpec(field="<inherit>", direction="desc"),
    )

    from compass_backend.planning.planner import merge_query_plan_with_context

    merged = merge_query_plan_with_context(
        followup,
        QueryContext(
            query_plan=previous,
            result_type="metric_lookup",
        ),
    )

    assert merged.selection == previous.selection
    assert merged.metrics == previous.metrics
    assert merged.sort == SortSpec(field="starting salary", direction="desc")


def test_merge_query_plan_can_inherit_prior_result_rows_as_selection() -> None:
    previous = QueryPlan(
        question="Show the five highest starting salaries.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        sort=SortSpec(field="starting salary", direction="desc"),
        limit={"count": 5, "kind": "top"},
        output=OutputSpec(format="table"),
    )
    followup = QueryPlan(
        inherit_from_session=True,
        inherit_selection_from="prior_result_rows",
        question="Sort those lowest first.",
        selection=SelectionSpec(scope="unspecified"),
        metrics=[],
        sort=SortSpec(field="<inherit>", direction="asc"),
    )

    from compass_backend.planning.planner import merge_query_plan_with_context

    merged = merge_query_plan_with_context(
        followup,
        QueryContext(
            query_plan=previous,
            result_type="metric_ranking",
            result_districts=[
                {"district_id": 2, "district_name": "Bravo", "state": "TX"},
                {"district_id": 1, "district_name": "Alpha", "state": "CA"},
            ],
        ),
    )

    assert merged.selection == SelectionSpec(
        scope="named_districts",
        districts=["Bravo, TX", "Alpha, CA"],
    )
    assert merged.metrics == previous.metrics
    assert merged.sort == SortSpec(field="starting salary", direction="asc")


def test_merge_query_plan_can_expand_prior_ranking_display_to_all_rows() -> None:
    previous = QueryPlan(
        question="Rank districts by enrollment, largest first.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="enrollment")],
        sort=SortSpec(field="enrollment", direction="desc"),
        output=OutputSpec(format="table"),
    )
    followup = QueryPlan(
        inherit_from_session=True,
        question="Show all rows.",
        selection=SelectionSpec(scope="unspecified"),
        metrics=[],
        output=OutputSpec(format="table", row_display="all"),
    )

    from compass_backend.planning.planner import merge_query_plan_with_context

    merged = merge_query_plan_with_context(
        followup,
        QueryContext(
            query_plan=previous,
            result_type="metric_ranking",
            row_count=133,
        ),
    )

    assert merged.selection == previous.selection
    assert merged.metrics == previous.metrics
    assert merged.limit is None
    assert merged.output == OutputSpec(format="table", row_display="all")


def test_merge_query_plan_reuses_peer_metric_for_prior_result_chart() -> None:
    previous = QueryPlan(
        operation="peer_comparison",
        question="Compare Denver peer sick leave policies.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Denver County School District 1"],
        ),
        metrics=[MetricSpec(name="sick leave policy")],
        output=OutputSpec(format="table"),
    )
    followup = QueryPlan(
        inherit_from_session=True,
        inherit_selection_from="prior_result_rows",
        question="Can you show me the sick days comparison in a graph?",
        selection=SelectionSpec(scope="unspecified"),
        metrics=[MetricSpec(name="sick days comparison")],
        output=OutputSpec(format="chart"),
    )

    from compass_backend.planning.planner import merge_query_plan_with_context

    merged = merge_query_plan_with_context(
        followup,
        QueryContext(
            query_plan=previous,
            result_type="peer_comparison",
            result_districts=[
                {
                    "district_id": 96,
                    "district_name": "Denver County School District 1",
                    "state": "CO",
                },
                {
                    "district_id": 97,
                    "district_name": "Jefferson County School District R-1",
                    "state": "CO",
                },
            ],
        ),
    )

    assert merged.operation == "lookup"
    assert merged.selection == SelectionSpec(
        scope="named_districts",
        districts=[
            "Denver County School District 1, CO",
            "Jefferson County School District R-1, CO",
        ],
    )
    assert merged.metrics == previous.metrics
    assert merged.output == OutputSpec(format="chart")


def test_merge_query_plan_prior_peer_rows_override_planner_named_selection() -> None:
    previous = QueryPlan(
        operation="peer_comparison",
        question="Compare Denver peer sick leave policies.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Denver Public Schools"],
        ),
        metrics=[MetricSpec(name="sick leave policy")],
        output=OutputSpec(format="table"),
    )
    followup = QueryPlan(
        operation="peer_comparison",
        inherit_from_session=True,
        inherit_selection_from="prior_result_rows",
        question="Show the sick days comparison in a graph.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=[
                "Denver Public Schools",
                "Aurora Public Schools",
            ],
        ),
        metrics=[MetricSpec(name="Maximum number of annual paid sick days")],
        output=OutputSpec(format="chart"),
    )

    from compass_backend.planning.planner import merge_query_plan_with_context

    merged = merge_query_plan_with_context(
        followup,
        QueryContext(
            query_plan=previous,
            result_type="peer_comparison",
            result_districts=[
                {
                    "district_id": 26,
                    "district_name": "Denver Public Schools",
                    "state": "CO",
                },
                {
                    "district_id": 24,
                    "district_name": "Aurora Public Schools",
                    "state": "CO",
                },
            ],
        ),
    )

    assert merged.operation == "lookup"
    assert merged.selection == SelectionSpec(
        scope="named_districts",
        districts=["Denver Public Schools, CO", "Aurora Public Schools, CO"],
    )
    assert merged.metrics == previous.metrics


def test_merge_query_plan_preserves_ordered_sort_steps_for_chart_followup() -> None:
    previous = QueryPlan(
        question="Rank the 10 largest districts by starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        sort_steps=[
            SortStepSpec(
                phase="selection",
                field="enrollment",
                direction="desc",
                key_type="profile_field",
                limit={"count": 10, "kind": "top"},
            ),
            SortStepSpec(
                phase="presentation",
                field="starting salary",
                direction="desc",
                key_type="policy_metric",
            ),
        ],
        output=OutputSpec(format="table"),
    )
    followup = QueryPlan(
        inherit_from_session=True,
        question="Make that a chart.",
        selection=SelectionSpec(scope="unspecified"),
        metrics=[],
        output=OutputSpec(format="chart"),
    )

    from compass_backend.planning.planner import merge_query_plan_with_context

    merged = merge_query_plan_with_context(
        followup,
        QueryContext(
            query_plan=previous,
            result_type="metric_ranking",
            result_districts=[
                {"district_id": 2, "district_name": "Bravo", "state": "TX"},
            ],
        ),
    )

    assert merged.sort_steps == previous.sort_steps
    assert merged.output == OutputSpec(format="chart")


def test_merge_turn_normalizes_resolved_metric_sort_for_context_followup() -> None:
    previous = QueryPlan(
        question="Compare starting salaries.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Chicago Public Schools", "Denver Public Schools"],
        ),
        metrics=[MetricSpec(name="starting salary")],
        output=OutputSpec(format="table"),
    )
    followup_plan = QueryPlan(
        question="Sort those highest first.",
        selection=previous.selection,
        metrics=[MetricSpec(name="starting salary")],
        sort=SortSpec(
            field="Annual base salary for a first year teacher with a bachelor's degree",
            direction="desc",
        ),
    )

    from compass_backend.planning.planner import merge_turn_with_session_context

    merged_turn = merge_turn_with_session_context(
        PlannerTurn(route="execute", confidence=0.9, query_plan=followup_plan),
        QueryContext(
            query_plan=previous,
            result_type="metric_lookup",
            result_metrics=[
                {
                    "metric_id": 89,
                    "metric_name": (
                        "Annual base salary for a first year teacher with a "
                        "bachelor's degree"
                    ),
                }
            ],
        ),
    )

    assert merged_turn.query_plan is not None
    assert merged_turn.query_plan.sort == SortSpec(
        field="starting salary",
        direction="desc",
    )


def test_merge_query_plan_drops_prior_rank_limit_for_named_lookup_followup() -> None:
    previous = QueryPlan(
        question="Show the five highest starting salaries.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
        sort=SortSpec(field="starting salary", direction="desc"),
        limit={"count": 5, "kind": "top"},
        output=OutputSpec(format="table"),
    )
    followup = QueryPlan(
        inherit_from_session=True,
        operation="lookup",
        question="Break those rows down by degree lane.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=["Bravo", "Charlie"],
        ),
        metrics=[
            MetricSpec(name="starting salary"),
            MetricSpec(name="master's starting salary", role="comparison"),
        ],
        output=OutputSpec(format="table"),
    )

    from compass_backend.planning.planner import merge_query_plan_with_context

    merged = merge_query_plan_with_context(
        followup,
        QueryContext(
            query_plan=previous,
            result_type="metric_ranking",
        ),
    )

    assert merged.operation == "lookup"
    assert merged.selection == followup.selection
    assert merged.metrics == followup.metrics
    assert merged.sort is None
    assert merged.limit is None


def test_fresh_chat_mvp_saves_turn_snapshot() -> None:
    store = InMemorySessionStore()
    turn = _direct_turn("Snapshot response.")
    agent = _agent_for_turn(turn)

    with TestClient(create_app(planner_agent=agent, session_store=store)) as client:
        response = client.post("/api/v1/chat/simple", json={"message": "snapshot me"})

    body = response.json()
    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])

    assert response.status_code == 200
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.snapshot_id == body["snapshot_id"]
    assert snapshot.turn_index == 1
    assert snapshot.user_message == "snapshot me"
    assert snapshot.assistant_message == "Snapshot response."
    assert snapshot.planner_turn == turn


def test_fresh_chat_snapshot_records_query_context_after_successful_execution() -> None:
    store = InMemorySessionStore()
    plan = QueryPlan(
        question="Rank districts by starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary")],
    )
    agent = _agent_for_turn(
        PlannerTurn(route="execute", confidence=0.9, query_plan=plan)
    )
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_ranking_result(),
            authority=ValidationAuthority(),
            message="Ranked districts by starting salary.",
        )
    )

    with TestClient(
        create_app(planner_agent=agent, session_store=store, query_executor=executor)
    ) as client:
        response = client.post("/api/v1/chat/simple", json={"message": "Rank salaries."})

    body = response.json()
    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])
    reloaded_session = anyio.run(store.load, body["session"]["session_id"])

    assert response.status_code == 200
    assert "query_context" not in body["session"]
    assert reloaded_session.query_context is not None
    assert reloaded_session.query_context.query_plan.question == plan.question
    assert reloaded_session.query_context.result_type == "metric_ranking"
    assert reloaded_session.query_context.row_count == 1
    assert reloaded_session.query_context.displayed_row_count == 1
    assert reloaded_session.query_context.display_limit is None
    assert reloaded_session.query_context.row_display == "preview"
    assert reloaded_session.query_context.data_limit_count is None
    assert reloaded_session.query_context.data_limit_kind is None
    assert reloaded_session.query_context.data_limit_source == "unbounded"
    assert reloaded_session.query_context.display_limit_source == "none"
    assert snapshots[0].query_context is not None
    assert snapshots[0].query_context.query_plan.question == plan.question
    assert snapshots[0].query_context.data_limit_source == "unbounded"


def test_fresh_chat_normalizes_profile_lookup_ranking_shape() -> None:
    store = InMemorySessionStore()
    plan = QueryPlan(
        operation="profile_lookup",
        question="Rank districts by enrollment, largest first.",
        selection=SelectionSpec(scope="all_covered_districts"),
        profile_fields=[ProfileFieldSpec(name="enrollment")],
        sort=SortSpec(field="enrollment", direction="desc"),
        output=OutputSpec(format="table"),
    )
    agent = _agent_for_turn(
        PlannerTurn(route="execute", confidence=0.9, query_plan=plan)
    )
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_profile_ranking_result(),
            authority=ValidationAuthority(),
            message="Ranked by NCES enrollment, highest to lowest.",
        )
    )

    with TestClient(
        create_app(planner_agent=agent, session_store=store, query_executor=executor)
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={"message": "Rank districts by enrollment, largest first."},
        )

    body = response.json()
    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])
    executed_plan = executor.plans[0]

    assert response.status_code == 200
    assert executed_plan.operation == "rank"
    assert [metric.name for metric in executed_plan.metrics] == ["enrollment"]
    assert executed_plan.profile_fields == []
    assert executed_plan.sort_steps == [
        SortStepSpec(
            phase="presentation",
            field="enrollment",
            direction="desc",
            key_type="profile_field",
        )
    ]
    assert snapshots[0].planner_turn.query_plan is not None
    assert snapshots[0].planner_turn.query_plan.operation == "rank"
    assert body["turn"]["query_plan"]["operation"] == "rank"


def test_fresh_chat_normalizes_rank_profile_fields_without_policy_metric() -> None:
    store = InMemorySessionStore()
    plan = QueryPlan(
        operation="rank",
        question="What are the largest districts in the database? Give me the top 10.",
        selection=SelectionSpec(scope="all_covered_districts"),
        profile_fields=[ProfileFieldSpec(name="enrollment")],
        sort=SortSpec(field="enrollment", direction="desc"),
        limit=LimitSpec(count=10, kind="top"),
        output=OutputSpec(format="table"),
    )
    agent = _agent_for_turn(
        PlannerTurn(route="execute", confidence=0.9, query_plan=plan)
    )
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_profile_ranking_result(),
            authority=ValidationAuthority(),
            message="Ranked by NCES enrollment, highest to lowest.",
        )
    )

    with TestClient(
        create_app(planner_agent=agent, session_store=store, query_executor=executor)
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={
                "message": (
                    "What are the largest districts in the database? "
                    "Give me the top 10."
                )
            },
        )

    body = response.json()
    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])
    executed_plan = executor.plans[0]

    assert response.status_code == 200
    assert executed_plan.selection == SelectionSpec(scope="all_covered_districts")
    assert [metric.name for metric in executed_plan.metrics] == ["enrollment"]
    assert executed_plan.profile_fields == []
    assert executed_plan.sort_steps == [
        SortStepSpec(
            phase="presentation",
            field="enrollment",
            direction="desc",
            key_type="profile_field",
            limit=LimitSpec(count=10, kind="top"),
        )
    ]
    assert snapshots[0].planner_turn.query_plan is not None
    assert snapshots[0].planner_turn.query_plan.metrics == executed_plan.metrics


def test_fresh_chat_normalizes_rank_profile_selection_for_policy_display() -> None:
    store = InMemorySessionStore()
    plan = QueryPlan(
        operation="lookup",
        question=(
            "Show me starting teacher salaries for districts with the highest "
            "free-and-reduced lunch share."
        ),
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting teacher salary")],
        profile_fields=[ProfileFieldSpec(name="free and reduced lunch share")],
        output=OutputSpec(format="table"),
    )
    agent = _agent_for_turn(
        PlannerTurn(route="execute", confidence=0.9, query_plan=plan)
    )
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_ranking_result(),
            authority=ValidationAuthority(),
            message="Ranked by free and reduced lunch share.",
        )
    )

    with TestClient(
        create_app(planner_agent=agent, session_store=store, query_executor=executor)
    ) as client:
        response = client.post(
            "/api/v1/chat/simple",
            json={
                "message": (
                    "Show me starting teacher salaries for districts with the "
                    "highest free-and-reduced lunch share."
                )
            },
        )

    body = response.json()
    snapshots = anyio.run(store.snapshots_for_session, body["session"]["session_id"])
    executed_plan = executor.plans[0]

    assert response.status_code == 200
    assert executed_plan.operation == "rank"
    assert executed_plan.sort_steps == [
        SortStepSpec(
            phase="selection",
            field="frpl_pct",
            direction="desc",
            key_type="profile_field",
            limit=LimitSpec(count=10, kind="top"),
        )
    ]
    assert snapshots[0].planner_turn.query_plan is not None
    assert snapshots[0].planner_turn.query_plan.sort_steps == executed_plan.sort_steps


def test_planner_turn_requires_route_matching_payload() -> None:
    try:
        PlannerTurn(
            route="execute",
            confidence=0.5,
            direct_response=DirectResponse(
                message="wrong payload",
                reason="wrong route",
            ),
        )
    except ValidationError as exc:
        assert "execute route requires its matching payload" in str(exc)
    else:
        raise AssertionError("PlannerTurn accepted a route without its payload")


# ---------------------------------------------------------------------------
# Regression: bare teacher-pay Vermont prompt commits with alternates (#1018)
# ---------------------------------------------------------------------------


def test_bare_teacher_pay_vermont_prompt_commits_with_alternates() -> None:
    """Regression for issue #1018: bare 'teacher pay in vermont' prompt should
    commit to first-year BA base salary (conventional default) with adjacent
    variants surfaced in manifest metadata, instead of deflecting to a
    multiple-choice clarify gate."""

    ma_metric = MetricCandidate(
        metric_id=11,
        name="teacher salary — first year, master's degree",
        answer_type="numeric",
    )
    max_ba_metric = MetricCandidate(
        metric_id=12,
        name="teacher salary — max, bachelor's degree new-hire schedule",
        answer_type="numeric",
    )

    turn = PlannerTurn(
        route="execute",
        confidence=0.87,
        query_plan=QueryPlan(
            operation="lookup",
            question="What is the teacher salary in Vermont?",
            selection=SelectionSpec(scope="state", states=["VT"]),
            metrics=[MetricSpec(name="teacher salary")],
            output=OutputSpec(format="table"),
        ),
    )
    executor = FakeQueryExecutor(
        ExecutionSuccess(
            result=_state_lookup_result("VT"),
            authority=ValidationAuthority(),
            message="Teacher salary in Vermont.",
            adjacent_candidates=[ma_metric, max_ba_metric],
        )
    )
    store = InMemorySessionStore()

    async def run_response():
        return await build_chat_response(
            ChatRequest(message="hey there - tell me how much teacher's are paid in vermont"),
            planner_agent=_agent_for_turn(turn),
            store=store,
            executor=executor,
            app_settings=Settings(session_store_backend="memory"),
        )

    response = anyio.run(run_response)

    # Route must be execute — not clarify.
    assert response.turn.route == "execute"

    # Manifest must carry adjacent_metrics with the two alternate labels.
    assert response.manifest is not None
    adjacents = response.manifest.metadata.get("adjacent_metrics") or []
    adjacent_labels = {entry["label"] for entry in adjacents}
    assert any("master" in label.lower() for label in adjacent_labels), (
        f"Expected a master's-degree label in adjacent_metrics, got: {adjacent_labels}"
    )
    assert any(
        "max" in label.lower() or "new-hire" in label.lower()
        for label in adjacent_labels
    ), f"Expected a max/new-hire label in adjacent_metrics, got: {adjacent_labels}"

    # The old clarify-gate f-string templates must NOT appear in the response body.
    assert "Reply with one of these options" not in response.message
    assert "I found a few Compass metrics that could match" not in response.message


# ---------------------------------------------------------------------------
# Regression: cross-concept clarify routes through stylist service (#1052)
# ---------------------------------------------------------------------------


def test_principal_pay_prompt_produces_stylist_voiced_clarify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #1052: a genuine cross-concept clarify (admin pay vs
    teacher-leader stipend) produces a stylist-composed question, not the
    deterministic f-string 'I found a few Compass metrics…'.

    Monkey-patches compose_clarify_question_async at both scoping and
    operations call sites so the test is deterministic without a real
    gateway. Verifies the WIRING: when the executor's ambiguous-metric path
    fires, it goes through the new service and the stylist's question reaches
    the chat response."""
    from compass_backend.catalog import CatalogAliasRecord
    from compass_backend.execution import (
        DeterministicQueryExecutor,
        MetricCandidate as _MetricCandidate,
    )

    composed_question = "Are you asking about admin pay or teacher-leader pay?"

    # --- fake stylist ----------------------------------------------------
    async def _fake_compose(
        metric_phrase: str,
        *,
        operation: str,
        candidates: list,
        adjudicator_hint: str | None = None,
        stylist_agent: object = None,
    ) -> ClarificationRequest:
        return ClarificationRequest(
            question=composed_question,
            missing_fields=["metric"],
            candidates=[c.name for c in candidates],
        )

    monkeypatch.setattr(
        "compass_backend.execution.scoping.compose_clarify_question_async",
        _fake_compose,
    )
    monkeypatch.setattr(
        "compass_backend.execution.operations.compose_clarify_question_async",
        _fake_compose,
    )

    # --- fake repository with ambiguous "principal pay" alias ------------
    principal_metric = _MetricCandidate(
        metric_id=100, name="Principal base salary", answer_type="numeric"
    )
    tl_metric = _MetricCandidate(
        metric_id=101, name="Teacher-leader stipend", answer_type="numeric"
    )

    ambiguous_alias = CatalogAliasRecord(
        alias="principal pay",
        normalized_alias="principal pay",
        entity_type="metric_bundle",
        resolution_status="ambiguous",
        candidate_refs=[{"metric_id": 100}, {"metric_id": 101}],
        source="test",
        provenance="regression-1052",
        scenario_ids=["SSN-1052"],
        review_status="approved",
    )

    class _MinimalRepo:
        async def search_metrics(self, query: str, *, limit: int = 5):
            return [principal_metric, tl_metric]

        async def fetch_metrics_by_ids(self, metric_ids: list) -> list:
            by_id = {100: principal_metric, 101: tl_metric}
            return [by_id[mid] for mid in metric_ids if mid in by_id]

        async def search_catalog_aliases(
            self, alias: str, *, entity_types: set
        ) -> list:
            if alias.casefold() == "principal pay":
                return [ambiguous_alias]
            return []

        async def fetch_metric_answer_rows(self, *, metric_id, academic_year):
            return []

        async def list_available_academic_years(self, *, metric_id, district_ids):
            return []

        async def fetch_metric_answer_rows_for_year(
            self, *, metric_id, academic_year, district_ids
        ):
            return []

        async def fetch_metric_answer_rows_for_years(
            self, *, metric_id, academic_years, district_ids
        ):
            return []

        async def fetch_reviewed_district_ids(self, *, academic_year, district_ids):
            return set()

        async def fetch_recent_metric_answer_rows(
            self, *, metric_id, before_academic_year, district_ids
        ):
            return []

        async def list_covered_districts(self, *, states=None):
            return []

        async def fetch_renderer_notes(self, note_keys):
            return []

    executor = DeterministicQueryExecutor(_MinimalRepo())
    store = InMemorySessionStore()

    turn = PlannerTurn(
        route="execute",
        confidence=0.82,
        query_plan=QueryPlan(
            operation="lookup",
            question="What is the principal pay in Vermont?",
            selection=SelectionSpec(scope="state", states=["VT"]),
            metrics=[MetricSpec(name="principal pay")],
            output=OutputSpec(format="table"),
        ),
    )

    async def run_response():
        return await build_chat_response(
            ChatRequest(message="what's the principal pay in vermont"),
            planner_agent=_agent_for_turn(turn),
            store=store,
            executor=executor,
            app_settings=Settings(session_store_backend="memory"),
        )

    response = anyio.run(run_response)

    # Route must be clarify — not execute.
    assert response.turn.route == "clarify"

    # The stylist-composed question must appear in the response body.
    assert composed_question in response.message

    # The old deterministic f-string question must NOT appear.
    assert "I found a few Compass metrics" not in response.message

    # The candidate list is preserved in the structured contract.
    assert response.turn.clarification is not None
    assert response.turn.clarification.candidates == [
        "Principal base salary",
        "Teacher-leader stipend",
    ]
