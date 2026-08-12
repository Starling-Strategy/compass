"""Tests for the NCTQ context resolver helpers."""

from __future__ import annotations

import pytest

from compass_backend.answer_layer.nctq_context import (
    NctqSnippet,
    has_prose_room,
    resolve_nctq_context,
    topic_keys_for_plan,
)
from compass_backend.contracts.planning import (
    MetricSpec,
    OutputSpec,
    QueryPlan,
    SelectionSpec,
)
from compass_backend.contracts.rendering import ResponseManifest


def test_nctq_snippet_format_includes_url_and_key_point() -> None:
    snippet = NctqSnippet(
        source_kind="rationale",
        title="Competitive Pay",
        url="https://www.nctq.org/district-policy-pathfinder/",
        summary_line="Districts should pay teachers competitively.",
        key_point="A $5,000 starting-salary bump cut vacancies 16% in one VA study.",
    )
    text = snippet.format()
    assert "[rationale] Competitive Pay" in text
    assert "URL: https://www.nctq.org/district-policy-pathfinder/" in text
    assert "Key point: A $5,000" in text


def test_nctq_snippet_format_omits_key_point_when_missing() -> None:
    snippet = NctqSnippet(
        source_kind="exemplar",
        title="District X benefits policy",
        url="https://example.org/x",
        summary_line="District X covers 100% of family premiums.",
        key_point=None,
    )
    text = snippet.format()
    assert "Key point" not in text


def test_has_prose_room_policy_guidance_returns_true() -> None:
    manifest = ResponseManifest(
        body="x",
        status="rendered",
        result_type="policy_guidance",
        validation_valid=True,
        metadata={},
    )
    assert has_prose_room(manifest, displayed_row_count=None) is True


def test_has_prose_room_small_row_count_returns_true() -> None:
    manifest = ResponseManifest(
        body="x",
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={},
    )
    assert has_prose_room(manifest, displayed_row_count=2) is True


def test_has_prose_room_full_ranking_returns_false() -> None:
    manifest = ResponseManifest(
        body="x",
        status="rendered",
        result_type="metric_ranking",
        validation_valid=True,
        metadata={},
    )
    assert has_prose_room(manifest, displayed_row_count=10) is False


def test_has_prose_room_caveat_count_triggers_true() -> None:
    manifest = ResponseManifest(
        body="x",
        status="rendered",
        result_type="metric_ranking",
        validation_valid=True,
        metadata={"answer_layer_caveat_count": 2},
    )
    assert has_prose_room(manifest, displayed_row_count=10) is True


class _FakeRepo:
    def __init__(self, snippets: tuple[NctqSnippet, ...]) -> None:
        self._snippets = snippets
        self.last_topic_keys: tuple[str, ...] | None = None

    async def fetch_snippets(
        self,
        topic_keys: tuple[str, ...],
        *,
        limit: int = 2,
    ) -> tuple[NctqSnippet, ...]:
        self.last_topic_keys = topic_keys
        return self._snippets[:limit]


def _plan_with_metric(name: str) -> QueryPlan:
    return QueryPlan(
        question="example",
        operation="rank",
        metrics=[MetricSpec(name=name)],
        selection=SelectionSpec(scope="all_covered_districts"),
        output=OutputSpec(),
    )


@pytest.mark.asyncio
async def test_resolve_nctq_context_returns_empty_when_no_prose_room() -> None:
    repo = _FakeRepo(snippets=(
        NctqSnippet(
            source_kind="rationale",
            title="Competitive Pay",
            url="https://example.org/x",
            summary_line="Districts should pay competitively.",
        ),
    ))
    manifest = ResponseManifest(
        body="x",
        status="rendered",
        result_type="metric_ranking",
        validation_valid=True,
        metadata={},
    )
    plan = _plan_with_metric("Starting salary")

    snippets = await resolve_nctq_context(
        plan,
        manifest=manifest,
        displayed_row_count=10,
        repo=repo,
    )

    assert snippets == ()


@pytest.mark.asyncio
async def test_resolve_nctq_context_returns_formatted_strings_when_gates_pass() -> None:
    repo = _FakeRepo(snippets=(
        NctqSnippet(
            source_kind="rationale",
            title="Competitive Pay",
            url="https://www.nctq.org/district-policy-pathfinder/",
            summary_line="Districts should pay competitively.",
        ),
    ))
    manifest = ResponseManifest(
        body="x",
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={},
    )
    plan = _plan_with_metric("Starting salary")

    snippets = await resolve_nctq_context(
        plan,
        manifest=manifest,
        displayed_row_count=1,
        repo=repo,
    )

    assert len(snippets) == 1
    assert "Competitive Pay" in snippets[0]
    assert "URL: https://www.nctq.org/district-policy-pathfinder/" in snippets[0]
    assert repo.last_topic_keys == ("general-salary",)


def test_topic_keys_for_plan_uses_metric_names() -> None:
    plan = _plan_with_metric("Starting salary")
    keys = topic_keys_for_plan(plan)
    assert keys == ("general-salary",)


def test_topic_keys_for_plan_returns_empty_for_unmatched_metric() -> None:
    plan = _plan_with_metric("enrollment")
    assert topic_keys_for_plan(plan) == ()


def test_topic_keys_for_plan_combines_multiple_metric_topics() -> None:
    plan = QueryPlan(
        question="example",
        operation="rank",
        metrics=[MetricSpec(name="Starting salary"), MetricSpec(name="Parental leave")],
        selection=SelectionSpec(scope="all_covered_districts"),
        output=OutputSpec(),
    )
    assert topic_keys_for_plan(plan) == ("general-salary", "leave")


def test_topic_keys_for_plan_dedupes_topics_across_metrics() -> None:
    plan = QueryPlan(
        question="example",
        operation="rank",
        metrics=[MetricSpec(name="Starting salary"), MetricSpec(name="Base salary")],
        selection=SelectionSpec(scope="all_covered_districts"),
        output=OutputSpec(),
    )
    assert topic_keys_for_plan(plan) == ("general-salary",)


@pytest.mark.asyncio
async def test_resolve_nctq_context_for_policy_guidance_returns_snippets() -> None:
    repo = _FakeRepo(snippets=(
        NctqSnippet(
            source_kind="rationale",
            title="Health Coverage",
            url="https://www.nctq.org/district-policy-pathfinder/",
            summary_line="Health coverage shapes recruiting.",
        ),
    ))
    manifest = ResponseManifest(
        body="x",
        status="rendered",
        result_type="policy_guidance",
        validation_valid=True,
        metadata={},
    )

    from compass_backend.answer_layer.nctq_context import (
        resolve_nctq_context_for_policy_guidance,
    )

    snippets = await resolve_nctq_context_for_policy_guidance(
        ("benefits",),
        manifest=manifest,
        displayed_row_count=None,
        repo=repo,
    )

    assert len(snippets) == 1
    assert "Health Coverage" in snippets[0]
    assert repo.last_topic_keys == ("benefits",)


@pytest.mark.asyncio
async def test_resolve_nctq_context_for_policy_guidance_returns_empty_for_no_topics() -> None:
    repo = _FakeRepo(snippets=())
    manifest = ResponseManifest(
        body="x",
        status="rendered",
        result_type="policy_guidance",
        validation_valid=True,
        metadata={},
    )

    from compass_backend.answer_layer.nctq_context import (
        resolve_nctq_context_for_policy_guidance,
    )

    snippets = await resolve_nctq_context_for_policy_guidance(
        (),
        manifest=manifest,
        displayed_row_count=None,
        repo=repo,
    )

    assert snippets == ()


def test_topic_keys_for_policy_guidance_dedupes() -> None:
    from compass_backend.answer_layer.nctq_context import (
        topic_keys_for_policy_guidance,
    )

    assert topic_keys_for_policy_guidance(("benefits", "benefits", "leave")) == (
        "benefits",
        "leave",
    )


@pytest.mark.asyncio
async def test_resolve_nctq_context_respects_limit_parameter() -> None:
    repo = _FakeRepo(snippets=(
        NctqSnippet(
            source_kind="rationale",
            title="Snippet A",
            url="https://example.org/a",
            summary_line="A",
        ),
        NctqSnippet(
            source_kind="exemplar",
            title="Snippet B",
            url="https://example.org/b",
            summary_line="B",
        ),
    ))
    manifest = ResponseManifest(
        body="x",
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={},
    )
    plan = _plan_with_metric("Starting salary")

    snippets = await resolve_nctq_context(
        plan,
        manifest=manifest,
        displayed_row_count=1,
        repo=repo,
        limit=1,
    )

    assert len(snippets) == 1
    assert "Snippet A" in snippets[0]


def test_has_prose_room_returns_false_for_zero_row_count() -> None:
    """displayed_row_count=0 means no rows shown; injecting NCTQ context
    into an empty answer feels like the system is dodging the miss."""
    manifest = ResponseManifest(
        body="x",
        status="rendered",
        result_type="metric_lookup",
        validation_valid=True,
        metadata={},
    )
    assert has_prose_room(manifest, displayed_row_count=0) is False
