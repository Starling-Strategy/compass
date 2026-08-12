"""Tests for fresh read-only DB adapters over the Compass navigator schema."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from compass_backend.catalog import TopicCandidate
from compass_backend.config import Settings
from compass_backend.db import ReadOnlyCatalogRepository, ReadOnlyPolicyAnswerRepository
from compass_backend.db.catalog import reset_alias_cache
from compass_backend.tests._chat_pool_fixtures import FakeChatPool, FakeConn

# Alias for in-file consistency: older fixture name → shared FakeConn.
FakeConnection = FakeConn


def _settings() -> Settings:
    return Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
    )


def _pool_for(connection: FakeConn) -> FakeChatPool:
    """Wrap a FakeConn in a FakeChatPool so a repo can borrow it directly."""

    return FakeChatPool(conn=connection)


@pytest.mark.asyncio
async def test_catalog_repository_fetch_all_metrics_for_resolver_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver fetch must return metrics ordered deterministically and read-only."""

    connection = FakeConnection(
        {
            "navigator_metrics": [
                {"metric_id": 39, "name": "Formal observations", "topic": "Evaluation", "answer_type": "numeric"},
                {"metric_id": 89, "name": "Starting salary", "topic": "Compensation", "answer_type": "numeric"},
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    metrics = await repository.fetch_all_metrics_for_resolver()

    assert [m.metric_id for m in metrics] == [39, 89]
    sql, _args = connection.queries[0]
    assert "navigator_metrics" in sql
    assert "ORDER BY" in sql.upper()
    assert "metric_id" in sql.lower()
    # Read-only: no INSERT/UPDATE/DELETE/CREATE
    assert all(verb not in sql.upper() for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE "))


@pytest.mark.asyncio
async def test_catalog_repository_searches_navigator_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_metrics": [
                {
                    "metric_id": 89,
                    "name": "Annual base salary for a first year teacher",
                    "topic": "Teacher Compensation",
                    "answer_type": "text",
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    metrics = await repository.search_metrics("salary", limit=3)

    assert metrics[0].metric_id == 89
    assert metrics[0].name == "Annual base salary for a first year teacher"
    assert metrics[0].answer_type == "text"
    sql, args = connection.queries[0]
    assert "navigator_metrics" in sql
    assert "policy_questions" not in sql
    assert "REPLACE(" not in sql
    assert args == ("salary", ["salary"], 3)


@pytest.mark.asyncio
async def test_catalog_repository_tokenizes_metric_search_phrases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_metrics": [
                {
                    "metric_id": 1234,
                    "name": "Average teacher starting salary",
                    "topic": "Teacher Compensation",
                    "answer_type": "numeric",
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    metrics = await repository.search_metrics("first-year teacher pay", limit=5)

    assert metrics[0].metric_id == 1234
    sql, args = connection.queries[0]
    assert "UNNEST($2::text[])" in sql
    assert "REPLACE(" not in sql
    assert args == ("first-year teacher pay", ["first", "pay"], 5)


@pytest.mark.asyncio
async def test_catalog_repository_metric_search_allows_high_overlap_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_metrics": [
                {
                    "metric_id": 233,
                    "name": (
                        "What percent of the employees' health insurance "
                        "premium does the district cover?"
                    ),
                    "topic": "Benefits & Retirement",
                    "answer_type": "numeric",
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    metrics = await repository.search_metrics(
        "employee health insurance premium contribution",
        limit=5,
    )

    assert metrics[0].metric_id == 233
    sql, args = connection.queries[0]
    assert "token_match_count" in sql
    assert "CEIL(CARDINALITY($2::text[]) * 0.6)::int" in sql
    assert args == (
        "employee health insurance premium contribution",
        ["employee", "health", "insurance", "premium", "contribution"],
        5,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "tokens"),
    [
        ("sick leave policy", ["sick", "leave"]),
        ("observation counts", ["observation", "counts"]),
        ("evaluation frequency for non-tenured teachers", ["evaluation", "frequency", "tenured"]),
    ],
)
async def test_catalog_repository_tokenizes_without_synonym_authority(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    tokens: list[str],
) -> None:
    connection = FakeConnection(
        {
            "navigator_metrics": [
                {
                    "metric_id": 39,
                    "name": "Minimum number of formal observations per evaluation cycle for non-tenured teachers",
                    "topic": "Evaluation",
                    "answer_type": "numeric",
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    await repository.search_metrics(query, limit=5)

    _, args = connection.queries[0]
    assert args == (query, tokens, 5)


def test_catalog_alias_migration_declares_governed_schema() -> None:
    sql = Path("src/compass_data_sync/migrations/007_catalog_aliases.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS compass.catalog_aliases" in sql
    assert "entity_type" in sql
    assert "resolution_status" in sql
    assert "canonical_ids JSONB" in sql
    assert "candidate_refs JSONB" in sql
    assert "review_status" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in sql


def test_renderer_notes_migration_declares_governed_note_schema() -> None:
    sql = Path("src/compass_data_sync/migrations/032_renderer_notes.sql").read_text()

    assert "ALTER TABLE compass.catalog_aliases" in sql
    assert "metadata JSONB NOT NULL DEFAULT '{}'::jsonb" in sql
    assert "CREATE TABLE IF NOT EXISTS compass.renderer_notes" in sql
    assert "note_key TEXT NOT NULL" in sql
    assert "note_text TEXT NOT NULL" in sql
    assert "review_status" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in sql


def test_catalog_alias_seed_is_idempotent_and_reviewed() -> None:
    sql = Path("src/compass_data_sync/seed_catalog_aliases.sql").read_text()

    assert "ON CONFLICT" in sql
    assert "INSERT INTO compass.renderer_notes" in sql
    assert "bachelor_starting_salary_default_lane" in sql
    assert "contextual_defaults" in sql
    assert "'{contextual_defaults}'" in sql
    assert "COALESCE(metadata->'contextual_defaults', '{}'::jsonb)" in sql
    assert "frpl_profile_sort_display" in sql
    assert "'sick leave', 'sick leave', 'metric_bundle', 'approved'" in sql
    assert "Issue #730 / #782 reviewed broad sick-leave phrase" in sql
    assert "sick leave policy" in sql
    assert "National Board Certification bonus" in sql
    assert "teacher starting salary" in sql
    assert "teacher working hours policy" in sql
    assert "teacher sick leave days" in sql
    assert "annual paid sick days for teachers" in sql
    assert "teacher benefits" in sql
    assert "paid parental leave beyond birthing parent" in sql
    assert "Feedback row 121 reviewed parental-leave eligibility" in sql
    assert "teacher evaluation policy" in sql
    assert "evaluation policy" in sql
    assert "teacher evaluation system" in sql
    assert "teacher evaluation framework" in sql
    assert "teacher evaluations differently" in sql
    assert "handle teacher evaluations differently" in sql
    assert "teacher evaluation criteria" in sql
    assert "teacher evaluation rating categories" in sql
    assert "teacher evaluation observation frequency" in sql
    assert "teacher evaluation ratings/levels" in sql
    assert "Evaluation Frequency - Non-Tenured Teachers" in sql
    assert "enrollment size" in sql
    assert "student enrollment" in sql
    assert "nces enrollment" in sql
    assert "free and reduced lunch share" in sql
    assert "free and reduced price lunch percentage" in sql
    assert "'nces_field'" in sql
    assert "starting salary for first-year teachers" in sql
    assert "starting salaries for teachers with a bachelor''s degree" in sql
    assert "BA starting salary" in sql
    assert "starting salary bachelor" in sql
    assert "master''s starting salary" in sql
    assert "starting pay master''s degree" in sql
    assert "maximum teacher salary" in sql
    assert "observation counts" in sql
    assert "number of classroom observations required for teacher evaluation" in sql
    assert "number of required classroom observations per year" in sql
    assert "teacher observation frequency policy" in sql
    assert "teachers watched in classroom regularly" in sql
    assert "instructional rounds or walkthroughs frequency" in sql
    assert "instructional walkthroughs policy" in sql
    assert "'math teacher stipend', 'math teacher stipend', 'metric', 'approved', '186'" in sql
    assert "'science teacher stipend', 'science teacher stipend', 'metric', 'approved', '185'" in sql
    assert "planning time" in sql
    assert "elementary planning time" in sql
    assert "middle school planning time" in sql
    assert "high school planning time" in sql
    assert "sick leave accumulation" in sql
    assert "sick leave carryover policy" in sql
    assert "DC Public Schools" in sql
    assert "minimum annual performance pay bonus" in sql
    assert "minimum performance pay bonus amount" in sql
    assert "maximum performance pay bonus amount" in sql
    assert "minimum and maximum performance pay bonus amounts" in sql
    assert "bonuses for being a highly effective teacher" in sql
    assert "pay linked to teacher quality" in sql
    assert "pay linked to school assignment" in sql
    assert "high-need school bonuses" in sql
    assert "DPS" in sql
    assert "ambiguous" in sql
    assert "SSN-235" in sql


def test_strike_legality_followup_migration_updates_aliases_and_existing_case() -> None:
    sql = Path(
        "src/compass_data_sync/migrations/044_strike_legality_followup.sql"
    ).read_text()

    assert "Legality of teacher strikes" in sql
    assert "Striking is permissible" in sql
    assert "scenario 361" in sql
    assert "SEL-MIGRATED-301-C00" in sql
    assert "Which states allow strikes?" in sql
    assert "expected_output" in sql


def test_m3_do_all_n_migration_seeds_pending_context_regression() -> None:
    sql = Path(
        "src/compass_data_sync/migrations/056_m3_do_all_n_pending_context.sql"
    ).read_text()

    assert "MULTI-TURN-2026-05-FOLLOWUP-007" in sql
    assert "do all 4 separately" in sql
    assert "PendingQueryContext" in sql
    assert "'scorecard_dimension', n.accuracy_primary_dimension" in sql
    assert "'github_issue', 769" in sql
    assert "'related_github_issue', 733" in sql


def test_m3_composite_executable_migration_extends_do_all_n_regression() -> None:
    """W2-M3-01 phase 3 (#859) seeds the executable composite-ranking shape
    alongside scenario 007's pending-context preservation regression."""

    sql = Path(
        "src/compass_data_sync/migrations/063_m3_composite_ranking_executable.sql"
    ).read_text()

    assert "MULTI-TURN-2026-05-FOLLOWUP-008" in sql
    assert "do all 4 separately" in sql
    assert "CompositeRankingResult" in sql
    assert "requires_composite_ranking" in sql
    assert "'executable_shape', true" in sql
    assert "'github_issue', 806" in sql
    assert "'related_github_issue', 769" in sql
    assert "'related_pr_chain', jsonb_build_array(852, 858, 859)" in sql


def test_feedback_121_parental_leave_alias_migration_is_governed() -> None:
    sql = Path(
        "src/compass_data_sync/migrations/083_parental_leave_beyond_birthing_aliases.sql"
    ).read_text()

    assert "paid parental leave beyond birthing parent" in sql
    assert '["216","218","219","220"]' in sql
    assert "feedback-row-121" in sql
    assert "ON CONFLICT (normalized_alias, entity_type, source)" in sql


@pytest.mark.asyncio
async def test_catalog_repository_searches_governed_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reset process-lifetime alias cache so this test gets its own DB load.
    reset_alias_cache()
    connection = FakeConnection(
        {
            "catalog_aliases": [
                {
                    "alias": "Dallas",
                    "normalized_alias": "dallas",
                    "entity_type": "district",
                    "resolution_status": "approved",
                    "canonical_id": "150",
                    "canonical_ids": "[]",
                    "candidate_refs": "[]",
                    "metadata": "{}",
                    "source": "SSN-235",
                    "provenance": "PR #488 reviewed evidence",
                    "scenario_ids": ["SSN-235"],
                    "review_status": "approved",
                    "active": True,
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    aliases = await repository.search_catalog_aliases(
        "Dallas ISD",
        entity_types={"district"},
    )

    assert aliases[0].alias == "Dallas"
    assert aliases[0].canonical_id == "150"
    sql, args = connection.queries[0]
    # The cache loader fetches ALL active+approved rows in one shot; the
    # normalized_alias / entity_type filtering happens in-memory.
    assert 'FROM "compass"."catalog_aliases"' in sql
    assert "navigator_metrics" not in sql
    assert args == ()  # cache loader passes no per-call filter args
    reset_alias_cache()


@pytest.mark.asyncio
async def test_catalog_repository_fetches_approved_renderer_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "renderer_notes": [
                {
                    "note_key": "bachelor_starting_salary_default_lane",
                    "note_text": "Use the bachelor's lane.",
                    "source": "SSN-235",
                    "provenance": "Reviewed disclosure.",
                    "scenario_ids": ["79"],
                    "review_status": "approved",
                    "active": True,
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    notes = await repository.fetch_renderer_notes(
        ["bachelor_starting_salary_default_lane"]
    )

    assert [note.note_key for note in notes] == ["bachelor_starting_salary_default_lane"]
    assert notes[0].note_text == "Use the bachelor's lane."
    sql, args = connection.queries[0]
    assert 'FROM "compass"."renderer_notes"' in sql
    assert "review_status = 'approved'" in sql
    assert args == (["bachelor_starting_salary_default_lane"],)


@pytest.mark.asyncio
async def test_catalog_repository_selects_districts_by_enrollment_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_districts": [
                {
                    "district_id": 2,
                    "district_name": "Bravo",
                    "state": "CA",
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    districts = await repository.select_districts_by_enrollment_range(
        states={"CA"},
        min_enrollment=20_000,
        max_enrollment=30_000,
        academic_year="2024 - 2025",
    )

    assert districts[0].district_id == 2
    sql, args = connection.queries[0]
    assert 'FROM "compass"."navigator_districts"' in sql
    assert "enrollment_authority.enrollment >= $3" in sql
    assert "enrollment_authority.enrollment <= $4" in sql
    assert args == ("2024 - 2025", ["CA"], 20_000, 30_000)


@pytest.mark.asyncio
async def test_catalog_repository_resolves_navigator_districts_by_normalized_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_districts": [
                {
                    "district_id": 10,
                    "district_name": "Alpha Public Schools",
                    "state": "CA",
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    resolution = await repository.resolve_districts(["Alpha"], states={"CA"})

    assert resolution.ok is True
    assert resolution.resolved[0].district_id == 10
    assert resolution.resolved[0].district_name == "Alpha Public Schools"
    assert resolution.resolved[0].state == "CA"
    sql, args = connection.queries[0]
    assert "navigator_districts" in sql
    assert "district_profiles" not in sql
    assert args == (["CA"],)


@pytest.mark.asyncio
async def test_catalog_repository_dedupes_repeated_navigator_district_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_districts": [
                {
                    "district_id": 10,
                    "district_name": "Alpha Public Schools",
                    "state": "CA",
                },
                {
                    "district_id": 10,
                    "district_name": "Alpha Public Schools",
                    "state": "CA",
                },
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    resolution = await repository.resolve_districts(["Alpha"])

    assert resolution.ok is True
    assert len(resolution.resolved) == 1
    assert resolution.resolved[0].district_id == 10


@pytest.mark.asyncio
async def test_catalog_repository_lists_covered_districts_for_state_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_districts": [
                {
                    "district_id": 10,
                    "district_name": "Alpha Public Schools",
                    "state": "CA",
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    districts = await repository.list_covered_districts(states={"CA"})

    assert districts[0].district_id == 10
    assert districts[0].district_name == "Alpha Public Schools"
    sql, args = connection.queries[0]
    assert 'FROM "compass"."navigator_districts"' in sql
    assert "district_profiles" not in sql
    assert args == (["CA"],)


@pytest.mark.asyncio
async def test_catalog_repository_searches_district_candidates_from_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "district_profiles": [
                {
                    "district_id": 50,
                    "district_name": "School District of Philadelphia",
                    "state": "PA",
                    "city": "Philadelphia",
                    "enrollment": 113443,
                    "locale_type": "City: Large",
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    candidates = await repository.search_district_candidates(
        "I am from Philadelphia",
        states={"PA"},
        limit=8,
    )

    assert [candidate.district_id for candidate in candidates] == [50]
    assert candidates[0].district_name == "School District of Philadelphia"
    assert candidates[0].state == "PA"
    sql, args = connection.queries[0]
    assert 'FROM "compass"."district_profiles"' in sql
    assert "navigator_districts" not in sql
    assert any(
        isinstance(arg, str) and "Philadelphia" in arg for arg in args
    )
    assert ["PA"] in args
    assert 8 in args


@pytest.mark.asyncio
async def test_catalog_repository_selects_largest_districts_by_current_enrollment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_districts": [
                {
                    "district_id": 20,
                    "district_name": "Bravo ISD",
                    "state": "TX",
                },
                {
                    "district_id": 10,
                    "district_name": "Alpha ISD",
                    "state": "TX",
                },
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    districts = await repository.select_largest_districts(
        states={"TX"},
        limit=2,
        academic_year="2024 - 2025",
    )

    assert [district.district_id for district in districts] == [20, 10]
    sql, args = connection.queries[0]
    assert 'FROM "compass"."navigator_districts"' in sql
    assert "enrollment DESC NULLS LAST" in sql
    assert "ay_range = $1" in sql
    assert "district_profiles" not in sql
    assert args == ("2024 - 2025", ["TX"], 2)


@pytest.mark.asyncio
async def test_catalog_repository_searches_navigator_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_sources": [
                {
                    "source_id": 700,
                    "district_id": 10,
                    "title": "Alpha Public Schools. CA. (2023-2025). Contract.",
                    "document_type": "Contract",
                    "academic_year": "2023 - 2025",
                    "url": "https://example.org/alpha-contract.pdf",
                    "is_download": True,
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    sources = await repository.search_source_documents(
        "contract",
        district_ids={10},
        limit=2,
    )

    assert sources[0].source_id == 700
    assert sources[0].district_id == 10
    assert sources[0].document_type == "Contract"
    assert sources[0].metadata == {"is_download": True}
    sql, args = connection.queries[0]
    assert 'FROM "compass"."navigator_sources"' in sql
    assert "nctq_publications" not in sql
    assert args == ("contract", [10], 2)


@pytest.mark.asyncio
async def test_catalog_repository_searches_topics_glossary_and_nces_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_topics": [
                {
                    "topic_id": 1,
                    "topic_name": "Teacher Compensation",
                    "subtopic_id": 11,
                    "subtopic_name": "Salary",
                    "question_count": 12,
                }
            ],
            "glossary": [
                {
                    "term_id": "collective bargaining",
                    "term": "Collective bargaining",
                    "definition": "Negotiation between employers and employees.",
                    "context": "District policy glossary",
                }
            ],
            "information_schema.columns": [
                {
                    "column_name": "enrollment",
                }
            ],
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    topics = await repository.search_topics("salary")
    glossary_terms = await repository.search_glossary_terms("collective")
    nces_fields = await repository.search_nces_fields("enrollment")

    assert topics[0].topic_name == "Teacher Compensation"
    assert topics[0].subtopic_name == "Salary"
    assert glossary_terms[0].term_id == "collective bargaining"
    assert glossary_terms[0].term == "Collective bargaining"
    assert nces_fields[0].field_key == "enrollment"
    assert nces_fields[0].data_type == "integer"
    assert [query[1][0] for query in connection.queries] == [
        "salary",
        "collective",
        "compass",
    ]
    # C2 (#1357): search_nces_fields now fetches all columns for the table once
    # and intersects in-memory, so the introspection query carries only the
    # schema parameter (no second `column_name = ANY($2)` arg).
    assert len(connection.queries[2][1]) == 1


@pytest.mark.asyncio
async def test_catalog_repository_exposes_virtual_frpl_profile_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection({"information_schema.columns": []})
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    nces_fields = await repository.search_nces_fields("frpl")

    assert [field.field_key for field in nces_fields] == ["frpl_pct"]


@pytest.mark.asyncio
async def test_catalog_repository_fetches_topic_metric_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_metrics": [
                {
                    "metric_id": 89,
                    "name": "Annual base salary for a first year teacher",
                    "topic": "Teacher Compensation",
                    "answer_type": "numeric",
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    metrics = await repository.fetch_topic_metric_candidates(
        [
            TopicCandidate(
                topic_id=1,
                topic_name="Teacher Compensation",
                subtopic_id=11,
                subtopic_name="Salary",
            )
        ],
        limit=3,
    )

    assert [metric.metric_id for metric in metrics] == [89]
    sql, args = connection.queries[0]
    assert 'FROM "compass"."navigator_metrics"' in sql
    assert "topic" in sql
    assert "subtopic" in sql
    assert args == (["Teacher Compensation"], ["Salary"], 3)


def test_topic_content_links_migration_declares_internal_related_content_table() -> None:
    sql = Path("src/compass_data_sync/migrations/010_topic_content_links.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS compass.topic_content_links" in sql
    assert "content_type IN" in sql
    assert "'metric'" in sql
    assert "'rationale'" in sql
    assert "'exemplar'" in sql
    assert "'publication'" in sql
    assert "'glossary'" in sql


@pytest.mark.asyncio
async def test_catalog_repository_looks_up_nces_only_profile_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "nces_districts": [
                {
                    "leaid": "1000680",
                    "nces_district_name": "Indian River School District",
                    "nces_state": "DE",
                    "profile_value": 10799,
                    "directory_year": 2022,
                    "finance_year": 2021,
                    "district_id": None,
                    "covered_district_name": None,
                    "covered_state": None,
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    rows = await repository.lookup_nces_profile_values(
        ["Indian river school district"],
        field_key="enrollment",
        states={"DE"},
        academic_year="2024 - 2025",
    )

    assert len(rows) == 1
    assert rows[0].district_id is None
    assert rows[0].district_name == "Indian River School District"
    assert rows[0].state == "DE"
    assert rows[0].field_key == "enrollment"
    assert rows[0].display_value == "10,799"
    assert rows[0].source == "compass.nces_districts"
    assert rows[0].provenance == "NCES directory year: 2022"
    assert rows[0].covered_by_compass is False
    sql, args = connection.queries[0]
    assert "nces_districts" in sql
    assert "navigator_nces_link" in sql
    assert args == ("2024 - 2025", ["DE"])


@pytest.mark.asyncio
async def test_catalog_repository_lists_covered_linked_nces_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "nces_districts": [
                {
                    "district_id": 26,
                    "district_name": "Denver Public Schools",
                    "state": "CO",
                    "city": "DENVER",
                    "enrollment": 87883,
                    "locale_text": "City: Large",
                    "latitude": 39.74575,
                    "longitude": -104.985751,
                    "total_rev_pp": 18381.59,
                    "total_exp_pp": 18137.07,
                    "pupil_teacher_ratio": 14.8,
                    "frpl_pct": None,
                    "directory_year": 2022,
                    "finance_year": 2021,
                }
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    rows = await repository.list_covered_nces_profiles(academic_year="2024 - 2025")

    assert len(rows) == 1
    assert rows[0].district_id == 26
    assert rows[0].district_name == "Denver Public Schools"
    assert rows[0].enrollment == 87883
    sql, args = connection.queries[0]
    assert "navigator_nces_link" in sql
    assert args == ("2024 - 2025",)


@pytest.mark.asyncio
async def test_policy_answer_repository_maps_navigator_answers_and_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_answers": [
                {
                    "answer_id": 501,
                    "district_id": 10,
                    "district_name": "Alpha Public Schools",
                    "state": "CA",
                    "metric_id": 89,
                    "metric_name": "Annual base salary for a first year teacher",
                    "value": "$56,370",
                    "academic_year": "2024 - 2025",
                }
            ],
            "navigator_citations": [],
            "navigator_sources": [
                {
                    "district_id": 10,
                    "source_name": "Alpha Public Schools. CA. (2023-2025). Contract.",
                    "source_url": "https://example.org/alpha-contract.pdf",
                    "source_document_type": "Contract",
                    "source_academic_year": "2023 - 2025",
                    "citation_order": 700,
                }
            ],
        }
    )
    repository = ReadOnlyPolicyAnswerRepository(_settings(), pool=_pool_for(connection))

    rows = await repository.fetch_metric_answer_rows(
        metric_id=89,
        academic_year="2024 - 2025",
    )

    assert len(rows) == 1
    assert rows[0].answer_id == 501
    assert rows[0].district_id == 10
    assert rows[0].metric_name == "Annual base salary for a first year teacher"
    assert rows[0].value == "$56,370"
    assert rows[0].citations[0].source_name == (
        "Alpha Public Schools. CA. (2023-2025). Contract."
    )
    assert rows[0].citations[0].academic_year == "2023 - 2025"
    assert rows[0].citations[0].citation_order == 700
    queried_tables = [
        table
        for sql, _args in connection.queries
        for table in (
            "navigator_answers",
            "navigator_citations",
            "navigator_sources",
        )
        if table in sql
    ]
    assert queried_tables == [
        "navigator_answers",
        "navigator_citations",
        "navigator_sources",
    ]
    assert all("policy_answers" not in sql for sql, _args in connection.queries)


@pytest.mark.asyncio
async def test_policy_answer_repository_skips_sources_for_no_answer_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1717: an "Issue not addressed" row has no value to back, so the fallback
    document attach must NOT dump the district's source pool onto it (the NYC
    "Total contracted workdays = Issue not addressed" 27-marker over-attachment).
    A source document backs a value; a no-answer row stays uncited."""
    connection = FakeConnection(
        {
            "navigator_answers": [
                {
                    "answer_id": 777,
                    "district_id": 10,
                    "district_name": "Alpha Public Schools",
                    "state": "CA",
                    "metric_id": 69,
                    "metric_name": "Total contracted workdays per academic year",
                    "value": "Issue not addressed",
                    "academic_year": "2024 - 2025",
                }
            ],
            "navigator_citations": [],
            "navigator_sources": [
                {
                    "district_id": 10,
                    "source_name": "Alpha Public Schools Annual Calendar, 2024-2025",
                    "source_url": "https://example.org/alpha-calendar.pdf",
                    "source_document_type": "Annual Calendar",
                    "source_academic_year": "2024 - 2025",
                    "citation_order": 800,
                }
            ],
        }
    )
    repository = ReadOnlyPolicyAnswerRepository(_settings(), pool=_pool_for(connection))

    rows = await repository.fetch_metric_answer_rows(
        metric_id=69,
        academic_year="2024 - 2025",
    )

    assert len(rows) == 1
    assert rows[0].value == "Issue not addressed"
    # No fallback source documents attached to the no-answer row.
    assert rows[0].citations == []


@pytest.mark.asyncio
async def test_policy_answer_repository_prefers_answer_level_navigator_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_answers": [
                {
                    "answer_id": 501,
                    "district_id": 10,
                    "district_name": "Alpha Public Schools",
                    "state": "CA",
                    "metric_id": 89,
                    "metric_name": "Annual base salary for a first year teacher",
                    "value": "$56,370",
                    "academic_year": "2024 - 2025",
                }
            ],
            "navigator_citations": [
                {
                    "answer_id": 501,
                    "source_name": (
                        "Alpha Public Schools. CA. (2024-2025). Contract. p. 7."
                    ),
                    "source_url": "https://example.org/answer-citation.pdf",
                    "citation_order": 1,
                }
            ],
            "navigator_sources": [
                {
                    "district_id": 10,
                    "source_name": "Alpha Public Schools. CA. (2023-2025). Contract.",
                    "source_url": "https://example.org/district-source.pdf",
                    "source_document_type": "Contract",
                    "source_academic_year": "2023 - 2025",
                    "citation_order": 700,
                }
            ],
        }
    )
    repository = ReadOnlyPolicyAnswerRepository(_settings(), pool=_pool_for(connection))

    rows = await repository.fetch_metric_answer_rows(
        metric_id=89,
        academic_year="2024 - 2025",
    )

    assert len(rows) == 1
    assert rows[0].citations[0].source_name == (
        "Alpha Public Schools. CA. (2024-2025). Contract. p. 7."
    )
    assert rows[0].citations[0].source_url == (
        "https://example.org/answer-citation.pdf"
    )
    assert rows[0].citations[0].citation_order == 1
    assert all(
        "navigator_sources" not in sql
        for sql, _args in connection.queries
    )


@pytest.mark.asyncio
async def test_policy_answer_repository_dedupes_answers_by_district(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_answers": [
                {
                    "answer_id": 501,
                    "district_id": 10,
                    "district_name": "Alpha Public Schools",
                    "state": "CA",
                    "metric_id": 89,
                    "metric_name": "Annual base salary for a first year teacher",
                    "value": "$50,000",
                    "academic_year": "2024 - 2025",
                },
                {
                    "answer_id": 502,
                    "district_id": 10,
                    "district_name": "Alpha Public Schools",
                    "state": "CA",
                    "metric_id": 89,
                    "metric_name": "Annual base salary for a first year teacher",
                    "value": "$56,370",
                    "academic_year": "2024 - 2025",
                },
            ],
            "navigator_sources": [],
        }
    )
    repository = ReadOnlyPolicyAnswerRepository(_settings(), pool=_pool_for(connection))

    rows = await repository.fetch_metric_answer_rows(
        metric_id=89,
        academic_year="2024 - 2025",
    )

    assert len(rows) == 1
    assert rows[0].answer_id == 502
    assert rows[0].value == "$56,370"


@pytest.mark.asyncio
async def test_policy_answer_repository_fetches_reviewed_district_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_answers": [
                {"district_id": 10},
                {"district_id": 11},
            ]
        }
    )
    repository = ReadOnlyPolicyAnswerRepository(_settings(), pool=_pool_for(connection))

    district_ids = await repository.fetch_reviewed_district_ids(
        academic_year="2024 - 2025",
        district_ids={10, 11, 12},
    )

    assert district_ids == {10, 11}
    sql, args = connection.queries[0]
    assert 'FROM "compass"."navigator_answers"' in sql
    assert "policy_answers" not in sql
    assert args == ("2024 - 2025", [10, 11, 12])


@pytest.mark.asyncio
async def test_policy_answer_repository_fetches_recent_prior_metric_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = FakeConnection(
        {
            "navigator_answers": [
                {
                    "answer_id": 401,
                    "district_id": 10,
                    "district_name": "Alpha Public Schools",
                    "state": "CA",
                    "metric_id": 89,
                    "metric_name": "Annual base salary for a first year teacher",
                    "value": "$55,000",
                    "academic_year": "2023 - 2024",
                }
            ]
        }
    )
    repository = ReadOnlyPolicyAnswerRepository(_settings(), pool=_pool_for(connection))

    rows = await repository.fetch_recent_metric_answer_rows(
        metric_id=89,
        before_academic_year="2024 - 2025",
        district_ids={10},
    )

    assert len(rows) == 1
    assert rows[0].answer_id == 401
    assert rows[0].academic_year == "2023 - 2024"
    assert rows[0].value == "$55,000"
    sql, args = connection.queries[0]
    assert 'FROM "compass"."navigator_answers"' in sql
    assert "policy_answers" not in sql
    assert args == (89, [10], "2024 - 2025")
