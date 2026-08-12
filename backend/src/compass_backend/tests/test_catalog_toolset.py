from __future__ import annotations

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from compass_backend.catalog import (
    CandidateCard,
    CompassCatalogToolset,
    RecallBatch,
    RecallReport,
)


class FakeRecallService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def recall(
        self,
        query: str,
        *,
        entity_types=None,
        limit: int = 10,
        states: set[str] | None = None,
    ) -> RecallReport:
        requested = tuple(entity_types or ("metric",))
        self.calls.append(
            {
                "query": query,
                "entity_types": requested,
                "limit": limit,
                "states": states,
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
                            label="Annual base salary for first-year BA teachers",
                            plain_definition="Approved catalog candidate.",
                            source_methods=["alias", "metric_search"],
                            entity_ref="metric:89",
                            debug_ref="metric:89",
                            metadata={"answer_type": "numeric"},
                        )
                    ],
                    limit=limit,
                )
            ],
        )


@pytest.mark.asyncio
async def test_catalog_toolset_returns_model_safe_advisory_cards() -> None:
    recall = FakeRecallService()
    toolset = CompassCatalogToolset(recall, max_limit=5)  # type: ignore[arg-type]

    result = await toolset.search_official_catalog_candidates(
        "starting salary",
        entity_types=["metric"],
        limit=50,
        states=["co"],
    )

    assert recall.calls == [
        {
            "query": "starting salary",
            "entity_types": ("metric",),
            "limit": 5,
            "states": {"CO"},
        }
    ]
    assert result.advisory_only is True
    assert "re-resolve" in result.authority_boundary
    assert result.candidates == [
        {
            "input_phrase": "starting salary",
            "entity_type": "metric",
            "label": "Annual base salary for first-year BA teachers",
            "plain_definition": "Approved catalog candidate.",
            "source_methods": ["alias", "metric_search"],
        }
    ]


@pytest.mark.asyncio
async def test_catalog_toolset_registers_only_candidate_search_tool() -> None:
    toolset = CompassCatalogToolset(FakeRecallService())  # type: ignore[arg-type]
    model = TestModel(call_tools=[])
    agent = Agent(model, toolsets=[toolset])

    result = await agent.run("Use catalog tools only if needed.")

    tool_names = [
        tool.name
        for tool in model.last_model_request_parameters.function_tools
    ]
    assert tool_names == ["search_official_catalog_candidates"]
    assert result.output == "success (no tool calls)"
