"""Optional live DB smoke tests for the fresh deterministic Compass pipeline."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import NamedTuple
from uuid import uuid4

import asyncpg
import pytest

from compass_backend.catalog import CatalogResolver
from compass_backend.config import Settings
from compass_backend.contracts import (
    DirectResponse,
    LimitSpec,
    MetricSpec,
    PlannerTurn,
    QueryPlan,
    SelectionSpec,
    TurnSnapshot,
)
from compass_backend.db import ReadOnlyCatalogRepository, ReadOnlyPolicyAnswerRepository
from compass_backend.db._pool import create_chat_pool
from compass_backend.execution import DeterministicQueryExecutor
from compass_backend.quality import validate_result
from compass_backend.rendering import render_response
from compass_backend.session import PostgresSessionStore

pytestmark = pytest.mark.live_db


class LiveLookupFixture(NamedTuple):
    metric_name: str
    district_names: list[str]


class LiveComparisonFixture(NamedTuple):
    metric_names: list[str]
    district_names: list[str]


def _live_settings(request: pytest.FixtureRequest) -> Settings:
    if not request.config.getoption("--run-live-db"):
        pytest.skip("pass --run-live-db to run live DB smoke tests")
    source = Settings()
    if not source.pg_password.get_secret_value():
        pytest.skip("PG_PASSWORD is required for live DB smoke tests")
    return source


@pytest.fixture
def live_settings(request: pytest.FixtureRequest) -> Settings:
    return _live_settings(request)


@pytest.fixture
async def live_pool(live_settings: Settings) -> AsyncIterator[asyncpg.Pool]:
    """Real staging connection pool for the live repos / session store.

    The read repos and ``PostgresSessionStore`` take a required keyword-only
    ``pool``; production wires the shared chat pool, so the live smoke uses the
    same ``create_chat_pool`` factory and closes it on teardown. (asyncio_mode
    is "auto", so this async fixture needs no extra decorator.)
    """
    pool = await create_chat_pool(live_settings)
    try:
        yield pool
    finally:
        await pool.close()


def _executor(
    settings: Settings, pool: asyncpg.Pool
) -> DeterministicQueryExecutor:
    return DeterministicQueryExecutor(
        ReadOnlyPolicyAnswerRepository(settings, pool=pool),
        catalog_resolver=CatalogResolver(
            ReadOnlyCatalogRepository(settings, pool=pool)
        ),
        current_academic_year=settings.current_academic_year,
    )


@pytest.mark.asyncio
async def test_live_catalog_candidate_smoke_uses_compass_schema(
    live_settings: Settings,
    live_pool: asyncpg.Pool,
) -> None:
    repository = ReadOnlyCatalogRepository(live_settings, pool=live_pool)

    source_docs = await repository.search_source_documents("contract", limit=1)
    topics = await repository.search_topics("salary", limit=1)
    glossary_terms = await repository.search_glossary_terms(
        "collective bargaining",
        limit=1,
    )
    nces_fields = await repository.search_nces_fields("enrollment", limit=1)

    assert source_docs
    assert source_docs[0].source_id > 0
    assert topics
    assert topics[0].topic_id > 0
    assert glossary_terms
    assert glossary_terms[0].term_id == "collective bargaining"
    assert nces_fields
    assert nces_fields[0].field_key == "enrollment"


@pytest.mark.asyncio
async def test_live_catalog_alias_authority_smoke_uses_compass_schema(
    live_settings: Settings,
    live_pool: asyncpg.Pool,
) -> None:
    repository = ReadOnlyCatalogRepository(live_settings, pool=live_pool)
    resolver = CatalogResolver(repository)

    district_aliases = await repository.search_catalog_aliases(
        "LAUSD",
        entity_types={"district"},
    )
    sick_leave = await resolver.resolve_metric_bundle("sick leave policy")

    assert district_aliases
    assert district_aliases[0].approved is True
    assert district_aliases[0].canonical_id == "15"
    assert sick_leave.ok is True
    assert [metric.metric_id for metric in sick_leave.resolved] == [198, 201, 202]


@pytest.mark.asyncio
async def test_live_ranking_smoke_orders_numeric_rows(
    live_settings: Settings,
    live_pool: asyncpg.Pool,
) -> None:
    metric_name = await _numeric_metric_name(live_settings)
    executor = _executor(live_settings, live_pool)
    plan = QueryPlan(
        question=f"Rank covered districts by {metric_name}.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name=metric_name)],
        limit=LimitSpec(count=5, kind="top"),
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_ranking"
    values = [row.value for row in outcome.result.rows]
    assert values == sorted(values, reverse=True)
    report = validate_result(plan, outcome.result)
    assert report.valid is True
    manifest = render_response(plan, outcome.result, report)
    assert manifest.status == "rendered"


@pytest.mark.asyncio
async def test_live_named_district_lookup_smoke_serializes_citations(
    live_settings: Settings,
    live_pool: asyncpg.Pool,
) -> None:
    fixture = await _lookup_fixture_with_sources(live_settings)
    executor = _executor(live_settings, live_pool)
    plan = QueryPlan(
        operation="lookup",
        question=f"What is {fixture.metric_name} for selected districts?",
        selection=SelectionSpec(
            scope="named_districts",
            districts=fixture.district_names,
        ),
        metrics=[MetricSpec(name=fixture.metric_name)],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_lookup"
    assert [row.district_name for row in outcome.result.rows] == sorted(
        fixture.district_names,
        key=str.casefold,
    )
    assert all(row.citation_markers for row in outcome.result.rows)
    assert outcome.result.citations
    report = validate_result(plan, outcome.result)
    assert report.valid is True
    manifest = render_response(plan, outcome.result, report)
    assert manifest.status == "rendered"
    assert "| Rank |" not in manifest.body


@pytest.mark.asyncio
async def test_live_multi_metric_lookup_comparison_smoke(
    live_settings: Settings,
    live_pool: asyncpg.Pool,
) -> None:
    fixture = await _comparison_fixture_with_sources(live_settings)
    executor = _executor(live_settings, live_pool)
    plan = QueryPlan(
        operation="lookup",
        question="Compare selected metrics for selected districts.",
        selection=SelectionSpec(
            scope="named_districts",
            districts=fixture.district_names,
        ),
        metrics=[
            MetricSpec(name=fixture.metric_names[0]),
            MetricSpec(name=fixture.metric_names[1], role="comparison"),
        ],
    )

    outcome = await executor.execute(plan)

    assert outcome.result is not None
    assert outcome.result.result_type == "metric_lookup"
    assert {row.metric_name for row in outcome.result.rows} == set(
        fixture.metric_names
    )
    assert {
        row.district_name for row in outcome.result.rows
    } == set(fixture.district_names)
    report = validate_result(plan, outcome.result)
    assert report.valid is True
    manifest = render_response(plan, outcome.result, report)
    assert manifest.status == "rendered"
    assert "| Rank |" not in manifest.body
    assert all(metric_name in manifest.body for metric_name in fixture.metric_names)


@pytest.mark.asyncio
async def test_live_postgres_session_store_smoke_persists_fresh_snapshot(
    live_settings: Settings,
    live_pool: asyncpg.Pool,
) -> None:
    session_id = f"fresh-session-smoke-{uuid4()}"
    store = PostgresSessionStore(live_settings, pool=live_pool)
    snapshot = TurnSnapshot(
        session_id=session_id,
        turn_index=1,
        user_message="live session smoke user message",
        assistant_message="live session smoke assistant message",
        planner_turn=PlannerTurn(
            route="direct",
            confidence=0.99,
            direct_response=DirectResponse(
                message="live session smoke assistant message",
                reason="Synthetic live DB session persistence smoke.",
            ),
        ),
    )

    try:
        initial = await store.load(session_id)
        saved = await store.save_turn(snapshot)
        reloaded = await store.load(session_id)
        snapshots = await store.snapshots_for_session(session_id)

        assert initial.turn_count == 0
        assert saved.turn_count == 1
        assert saved.latest_snapshot_id == snapshot.snapshot_id
        assert reloaded.turn_count == 1
        assert reloaded.latest_snapshot_id == snapshot.snapshot_id
        assert len(snapshots) == 1
        assert snapshots[0] == snapshot
    finally:
        await _delete_live_session(live_settings, session_id)


async def _numeric_metric_name(settings: Settings) -> str:
    sql = f"""
        SELECT metric.name
        FROM {_table(settings, "navigator_metrics")} metric
        INNER JOIN {_table(settings, "navigator_answers")} answer
            ON answer.metric_id = metric.metric_id
        WHERE answer.ay_range = $1
          AND answer.value ~
              '^[[:space:]]*[$]?[[:space:]]*-?[0-9][0-9,]*([.][0-9]+)?[[:space:]]*%?[[:space:]]*$'
        GROUP BY metric.metric_id, metric.name
        ORDER BY COUNT(*) DESC, metric.metric_id
        LIMIT 1
    """
    conn = await _connect(settings)
    try:
        row = await conn.fetchrow(sql, settings.current_academic_year)
    finally:
        await conn.close()
    if row is None:
        pytest.skip("no rankable navigator metric is available for live smoke")
    return str(row["name"])


async def _lookup_fixture_with_sources(settings: Settings) -> LiveLookupFixture:
    sql = f"""
        WITH candidate_metric AS (
            SELECT answer.metric_id, metric.name
            FROM {_table(settings, "navigator_answers")} answer
            INNER JOIN {_table(settings, "navigator_metrics")} metric
                ON metric.metric_id = answer.metric_id
            WHERE answer.ay_range = $1
              AND EXISTS (
                  SELECT 1
                  FROM {_table(settings, "navigator_sources")} source
                  WHERE source.district_id = answer.district_id
                    AND (
                        source.valid_year_range LIKE '%' || split_part($1, ' - ', 1) || '%'
                        OR source.valid_year_range LIKE '%' || split_part($1, ' - ', 2) || '%'
                    )
              )
            GROUP BY answer.metric_id, metric.name
            HAVING COUNT(DISTINCT answer.district_id) >= 2
            ORDER BY answer.metric_id
            LIMIT 1
        )
        SELECT cm.name, district.name AS district_name
        FROM candidate_metric cm
        INNER JOIN {_table(settings, "navigator_answers")} answer
            ON answer.metric_id = cm.metric_id
           AND answer.ay_range = $1
        INNER JOIN {_table(settings, "navigator_districts")} district
            ON district.district_id = answer.district_id
        WHERE EXISTS (
            SELECT 1
            FROM {_table(settings, "navigator_sources")} source
            WHERE source.district_id = answer.district_id
              AND (
                  source.valid_year_range LIKE '%' || split_part($1, ' - ', 1) || '%'
                  OR source.valid_year_range LIKE '%' || split_part($1, ' - ', 2) || '%'
              )
        )
        GROUP BY cm.name, district.name
        ORDER BY district.name
        LIMIT 2
    """
    conn = await _connect(settings)
    try:
        rows = await conn.fetch(sql, settings.current_academic_year)
    finally:
        await conn.close()
    if len(rows) < 2:
        pytest.skip("no sourced two-district lookup fixture is available")
    return LiveLookupFixture(
        metric_name=str(rows[0]["name"]),
        district_names=[str(row["district_name"]) for row in rows],
    )


async def _comparison_fixture_with_sources(
    settings: Settings,
) -> LiveComparisonFixture:
    metric_sql = f"""
        WITH sourced_answers AS (
            SELECT
                answer.metric_id,
                metric.name AS metric_name,
                answer.district_id
            FROM {_table(settings, "navigator_answers")} answer
            INNER JOIN {_table(settings, "navigator_metrics")} metric
                ON metric.metric_id = answer.metric_id
            WHERE answer.ay_range = $1
              AND EXISTS (
                  SELECT 1
                  FROM {_table(settings, "navigator_sources")} source
                  WHERE source.district_id = answer.district_id
                    AND (
                        source.valid_year_range LIKE '%' || split_part($1, ' - ', 1) || '%'
                        OR source.valid_year_range LIKE '%' || split_part($1, ' - ', 2) || '%'
                  )
              )
        )
        SELECT
            metric_id,
            metric_name,
            ARRAY_AGG(DISTINCT district_id) AS district_ids
        FROM sourced_answers
        GROUP BY metric_id, metric_name
        HAVING COUNT(DISTINCT district_id) >= 2
        ORDER BY COUNT(DISTINCT district_id) DESC, metric_id
        LIMIT 25
    """
    conn = await _connect(settings)
    try:
        metric_rows = await conn.fetch(metric_sql, settings.current_academic_year)
    finally:
        await conn.close()

    selected_pair: tuple[object, object, list[int]] | None = None
    for left_index, left in enumerate(metric_rows):
        left_district_ids = set(left["district_ids"])
        for right in metric_rows[left_index + 1 :]:
            common_district_ids = sorted(
                left_district_ids & set(right["district_ids"])
            )
            if len(common_district_ids) >= 2:
                selected_pair = (left, right, common_district_ids[:2])
                break
        if selected_pair is not None:
            break

    if selected_pair is None:
        pytest.skip("no sourced two-metric comparison fixture is available")

    left, right, district_ids = selected_pair
    district_sql = f"""
        SELECT district_id, name AS district_name
        FROM {_table(settings, "navigator_districts")}
        WHERE district_id = ANY($1::int[])
        GROUP BY district_id, name
        ORDER BY name
    """
    conn = await _connect(settings)
    try:
        district_rows = await conn.fetch(district_sql, district_ids)
    finally:
        await conn.close()
    if len(district_rows) < 2:
        pytest.skip("no named districts are available for comparison fixture")

    return LiveComparisonFixture(
        metric_names=[str(left["metric_name"]), str(right["metric_name"])],
        district_names=[str(row["district_name"]) for row in district_rows],
    )


async def _connect(settings: Settings) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password.get_secret_value(),
        command_timeout=settings.pg_command_timeout,
    )


def _table(settings: Settings, table_name: str) -> str:
    return f"{_quote_ident(settings.pg_schema)}.{_quote_ident(table_name)}"


async def _delete_live_session(settings: Settings, session_id: str) -> None:
    conn = await _connect(settings)
    try:
        await conn.execute(
            f"DELETE FROM {_table(settings, 'chat_messages')} WHERE session_id = $1",
            session_id,
        )
        await conn.execute(
            f"DELETE FROM {_table(settings, 'chat_sessions')} WHERE session_id = $1",
            session_id,
        )
    finally:
        await conn.close()


def _quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@pytest.mark.asyncio
async def test_live_frpl_profile_lookup_resolves_navigator_only_field(
    live_settings: Settings,
    live_pool: asyncpg.Pool,
) -> None:
    """frpl_pct lives only in navigator_districts; lookup_nces_profile_values must
    source it via the district_id link instead of nces.<field_key> (#1720 follow-up).

    Regression for the "I could not resolve sourced NCES/profile values" refusal:
    Brevard Public Schools has frpl data for every year, so the lookup must return a
    value, a percent display, and a navigator-vintage provenance.
    """
    repository = ReadOnlyCatalogRepository(live_settings, pool=live_pool)

    rows = await repository.lookup_nces_profile_values(
        ["Brevard Public Schools"],
        field_key="frpl_pct",
        states={"FL"},
        academic_year=live_settings.current_academic_year,
    )

    assert len(rows) == 1, "frpl_pct must resolve from navigator_districts"
    row = rows[0]
    assert row.field_key == "frpl_pct"
    assert row.value is not None
    assert 0 <= float(row.value) <= 100
    assert row.display_value.endswith("%")
    assert "profile year" in row.provenance.lower()
    assert row.covered_by_compass is True
