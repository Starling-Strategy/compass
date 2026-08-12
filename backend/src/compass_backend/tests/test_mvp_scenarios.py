"""Scenario-level tests through planner, catalog, execution, validation, rendering."""

from __future__ import annotations

from typing import NamedTuple

from fastapi.testclient import TestClient
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from compass_backend.api.app import create_app
from compass_backend.artifacts import CitationSource
from compass_backend.catalog import (
    CatalogAliasRecord,
    DistrictCandidate,
    DistrictResolution,
    MetricCandidate,
    normalize_district_name_for_resolution,
)
from compass_backend.contracts import (
    LimitSpec,
    MetricSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.config import Settings
from compass_backend.execution import DeterministicQueryExecutor, MetricAnswerRow
from compass_backend.session import InMemorySessionStore


def _create_offline_app(*args, app_settings: Settings | None = None, **kwargs):
    """Keep fake-repository scenarios isolated from live catalog dependencies."""

    base = app_settings or Settings(session_store_backend="memory")
    return create_app(
        *args,
        app_settings=base.model_copy(
            update={
                "catalog_recall_shadow_enabled": False,
                "catalog_resolver_recall_enabled": False,
            }
        ),
        **kwargs,
    )


class ScenarioRepository:
    """Fake catalog and answer repository for full fresh-pipeline scenarios."""

    def __init__(self) -> None:
        self.metrics = {
            "Annual base salary for a first year teacher with a bachelor's degree": MetricCandidate(
                metric_id=89,
                name=(
                    "Annual base salary for a first year teacher with a "
                    "bachelor's degree"
                ),
                answer_type="numeric",
            ),
            "Collective bargaining status": MetricCandidate(
                metric_id=2002,
                name="Collective bargaining status",
                answer_type="text",
            ),
        }
        self.districts = [
            DistrictCandidate(district_id=1, district_name="Alpha", state="CA"),
            DistrictCandidate(district_id=2, district_name="Bravo", state="CA"),
            DistrictCandidate(district_id=3, district_name="Charlie", state="CA"),
        ]
        self.rows = {
            89: [
                _answer_row(1, "Alpha", "$50,000", metric_id=89, answer_id=101),
                _answer_row(2, "Bravo", "$70,000", metric_id=89, answer_id=102),
                _answer_row(3, "Charlie", "$60,000", metric_id=89, answer_id=103),
            ],
            2002: [
                _answer_row(1, "Alpha", "Yes", metric_id=2002, answer_id=201),
                _answer_row(3, "Charlie", "No", metric_id=2002, answer_id=203),
            ],
        }
        self.metric_queries: list[str] = []
        self.district_queries: list[list[str]] = []
        self.fetched_metric_ids: list[int] = []

    async def fetch_metrics_by_ids(
        self,
        metric_ids: list[int],
    ) -> list[MetricCandidate]:
        self.metric_queries.append(
            "ids:" + ",".join(str(metric_id) for metric_id in metric_ids)
        )
        metrics_by_id = {metric.metric_id: metric for metric in self.metrics.values()}
        return [
            metrics_by_id[metric_id]
            for metric_id in metric_ids
            if metric_id in metrics_by_id
        ]

    async def search_catalog_aliases(
        self,
        alias: str,
        *,
        entity_types: set[str],
    ) -> list[CatalogAliasRecord]:
        if "metric" not in entity_types:
            return []
        if normalize_district_name_for_resolution(alias) != "starting salary":
            return []
        return [
            CatalogAliasRecord(
                alias="starting salary",
                normalized_alias="starting salary",
                entity_type="metric",
                resolution_status="approved",
                canonical_id="89",
                source="test",
                provenance="scenario fixture",
                scenario_ids=[],
                review_status="approved",
            )
        ]

    async def search_metrics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[MetricCandidate]:
        self.metric_queries.append(query)
        metric = self.metrics.get(query)
        return [metric] if metric is not None else []

    async def resolve_districts(
        self,
        names: list[str],
        *,
        states: set[str] | None = None,
    ) -> DistrictResolution:
        self.district_queries.append(names)
        state_filter = {state.upper() for state in states or set()}
        resolved: list[DistrictCandidate] = []
        unresolved: list[str] = []
        for name in names:
            normalized_name = normalize_district_name_for_resolution(name)
            matches = [
                district
                for district in self.districts
                if normalize_district_name_for_resolution(district.district_name)
                == normalized_name
                and (not state_filter or (district.state or "").upper() in state_filter)
            ]
            if len(matches) == 1:
                resolved.append(matches[0])
            else:
                unresolved.append(name)
        return DistrictResolution(resolved=resolved, unresolved=unresolved)

    async def list_covered_districts(
        self,
        *,
        states: set[str] | None = None,
    ) -> list[DistrictCandidate]:
        state_filter = {state.upper() for state in states or set()}
        return [
            district
            for district in self.districts
            if not state_filter or (district.state or "").upper() in state_filter
        ]

    async def fetch_metric_answer_rows(
        self,
        *,
        metric_id: int,
        academic_year: str,
    ) -> list[MetricAnswerRow]:
        self.fetched_metric_ids.append(metric_id)
        return [
            row.model_copy(update={"academic_year": academic_year})
            for row in self.rows.get(metric_id, [])
        ]

    async def fetch_reviewed_district_ids(
        self,
        *,
        academic_year: str,
        district_ids: set[int],
    ) -> set[int]:
        return {
            row.district_id
            for rows in self.rows.values()
            for row in rows
            if row.academic_year == academic_year and row.district_id in district_ids
        }

    async def fetch_recent_metric_answer_rows(
        self,
        *,
        metric_id: int,
        before_academic_year: str,
        district_ids: set[int],
    ) -> list[MetricAnswerRow]:
        return []


class ScenarioRun(NamedTuple):
    body: dict
    repository: ScenarioRepository


def _answer_row(
    district_id: int,
    district_name: str,
    value: object,
    *,
    metric_id: int,
    answer_id: int,
) -> MetricAnswerRow:
    return MetricAnswerRow(
        answer_id=answer_id,
        district_id=district_id,
        district_name=district_name,
        state="CA",
        metric_id=metric_id,
        metric_name="ignored raw metric name",
        value=value,
        academic_year="2024 - 2025",
        citations=[
            CitationSource(
                source_name=(
                    f"{district_name} District. CA. (2024-2025). Contract. p. 1."
                ),
                source_url=f"https://example.org/{district_name.casefold()}.pdf",
                document_type=None,
                academic_year="2024 - 2025",
                district_id=district_id,
                citation_order=1,
            )
        ],
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


def _run_scenario(turn: PlannerTurn, message: str) -> ScenarioRun:
    repository = ScenarioRepository()
    executor = DeterministicQueryExecutor(repository)
    with TestClient(
        _create_offline_app(
            planner_agent=_agent_for_turn(turn),
            query_executor=executor,
            session_store=InMemorySessionStore(),
        )
    ) as client:
        response = client.post("/api/v1/chat/simple", json={"message": message})
    assert response.status_code == 200
    return ScenarioRun(body=response.json(), repository=repository)


def test_scenario_ranking_preserves_numeric_sort_order_and_citations() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            question="Rank covered districts by starting salary.",
            selection=SelectionSpec(scope="all_covered_districts"),
            metrics=[MetricSpec(name="starting salary")],
            limit=LimitSpec(count=2, kind="top"),
        ),
    )

    run = _run_scenario(turn, "Rank covered districts by starting salary.")
    body = run.body

    assert body["result"]["result_type"] == "metric_ranking"
    assert run.repository.metric_queries == [
        "starting salary",
        "ids:89",
    ]
    assert run.repository.district_queries == []
    assert run.repository.fetched_metric_ids == [89]
    assert [row["district_name"] for row in body["result"]["rows"]] == [
        "Bravo",
        "Charlie",
    ]
    assert [row["value"] for row in body["result"]["rows"]] == [70000.0, 60000.0]
    assert {row["metric_id"] for row in body["result"]["rows"]} == {89}
    assert {
        row["metric_name"] for row in body["result"]["rows"]
    } == {"Annual base salary for a first year teacher with a bachelor's degree"}
    assert body["validation"]["valid"] is True
    assert body["result"]["rows"][0]["citation_markers"] == [1]
    assert body["result"]["citations"][0]["district_id"] == 2
    assert body["manifest"]["status"] == "rendered"


def test_scenario_named_district_lookup_uses_value_table_not_ranking() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="lookup",
            question="What is collective bargaining status for Charlie and Alpha?",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["Charlie", "Alpha"],
            ),
            metrics=[MetricSpec(name="Collective bargaining status")],
        ),
    )

    run = _run_scenario(
        turn,
        "What is collective bargaining status for Charlie and Alpha?",
    )
    body = run.body

    assert body["result"]["result_type"] == "metric_lookup"
    assert run.repository.metric_queries == ["Collective bargaining status"]
    assert run.repository.district_queries == [["Charlie", "Alpha"]]
    assert run.repository.fetched_metric_ids == [2002]
    assert body["result"]["selection"]["scope"] == "named_districts"
    assert {
        district["district_id"]
        for district in body["result"]["selection"]["districts"]
    } == {1, 3}
    assert [row["district_name"] for row in body["result"]["rows"]] == [
        "Alpha",
        "Charlie",
    ]
    assert [row["display_value"] for row in body["result"]["rows"]] == ["Yes", "No"]
    assert {row["metric_id"] for row in body["result"]["rows"]} == {2002}
    assert body["result"]["rows"][0]["citation_markers"] == [1]
    assert body["result"]["rows"][1]["citation_markers"] == [2]
    assert "| Rank |" not in body["message"]
    assert (
        "| District | State | Academic year | Collective bargaining status | Sources |"
        in body["message"]
    )
    assert body["validation"]["valid"] is True


def test_scenario_named_district_comparison_uses_catalog_ids_and_pivot_table() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="lookup",
            question="Compare collective bargaining and starting salary for Charlie and Alpha.",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["Charlie", "Alpha"],
            ),
            metrics=[
                MetricSpec(name="Collective bargaining status"),
                MetricSpec(name="starting salary", role="comparison"),
            ],
        ),
    )

    run = _run_scenario(
        turn,
        "Compare collective bargaining and starting salary for Charlie and Alpha.",
    )
    body = run.body

    assert body["result"]["result_type"] == "metric_lookup"
    assert run.repository.metric_queries == [
        "Collective bargaining status",
        "starting salary",
        "ids:89",
    ]
    assert run.repository.district_queries == [["Charlie", "Alpha"]]
    assert run.repository.fetched_metric_ids == [2002, 89]
    assert [
        (row["district_name"], row["metric_name"], row["display_value"])
        for row in body["result"]["rows"]
    ] == [
        (
            "Alpha",
            "Annual base salary for a first year teacher with a bachelor's degree",
            "$50,000",
        ),
        ("Alpha", "Collective bargaining status", "Yes"),
        (
            "Charlie",
            "Annual base salary for a first year teacher with a bachelor's degree",
            "$60,000",
        ),
        ("Charlie", "Collective bargaining status", "No"),
    ]
    assert len(body["result"]["citations"]) == 2
    assert body["validation"]["valid"] is True
    assert "| Rank |" not in body["message"]
    assert (
        "| District | State | Annual base salary for a first year teacher with a bachelor's degree | "
        "Collective bargaining status | Sources |"
    ) in body["message"]
    assert "| Alpha | CA | $50,000 | Yes | [1] |" in body["message"]
    assert "| Charlie | CA | $60,000 | No | [2] |" in body["message"]


def test_scenario_unresolved_named_district_returns_out_of_universe_artifact() -> None:
    turn = PlannerTurn(
        route="execute",
        confidence=0.9,
        query_plan=QueryPlan(
            operation="lookup",
            question="What is starting salary for Unknown District?",
            selection=SelectionSpec(
                scope="named_districts",
                districts=["Unknown District"],
            ),
            metrics=[MetricSpec(name="starting salary")],
        ),
    )

    run = _run_scenario(turn, "What is starting salary for Unknown District?")

    assert run.body["result"]["result_type"] == "metric_lookup"
    assert run.body["result"]["rows"][0]["district_id"] is None
    assert run.body["result"]["rows"][0]["district_name"] == "Unknown District"
    assert run.body["result"]["rows"][0]["coverage_state"] == "out_of_universe"
    # #1514: the out-of-Pathfinder name never becomes a table cell — it is
    # voiced with the one canonical sentence; the short label is retired.
    assert "is not in the District Policy Pathfinder" in run.body["message"]
    assert "Out of Pathfinder" not in run.body["message"]
    assert run.body["validation"]["valid"] is True
    assert run.body["manifest"]["status"] == "rendered"
    assert run.repository.district_queries == [["Unknown District"]]
    assert run.repository.metric_queries == [
        "starting salary",
        "ids:89",
    ]
    assert run.repository.fetched_metric_ids == [89]
