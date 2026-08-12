from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_sync_run_categories_migration_declares_required_columns():
    sql = Path("src/compass_data_sync/migrations/008_sync_run_categories.sql").read_text()
    pathfinder_sql = Path(
        "src/compass_data_sync/migrations/009_pathfinder_content_cache.sql"
    ).read_text()
    schema_sql = Path("src/compass_data_sync/compass_schema.sql").read_text()

    assert "CREATE TABLE IF NOT EXISTS compass.sync_run_categories" in sql
    assert "CREATE TABLE IF NOT EXISTS compass.sync_run_categories" in schema_sql
    assert "CREATE TABLE IF NOT EXISTS compass.pathfinder_content_items" in pathfinder_sql
    assert "CREATE TABLE IF NOT EXISTS compass.pathfinder_content_items" in schema_sql
    assert "summary_facts" in schema_sql
    assert "completed_with_warnings" in schema_sql
    assert "ALTER TABLE compass.sync_runs" in sql
    for column in [
        "run_id",
        "category",
        "source_system",
        "status",
        "rows_inserted",
        "rows_updated",
        "rows_deleted",
        "rows_unchanged",
        "row_count_after",
        "source_freshness_at",
        "database_freshness_at",
        "last_successful_refresh_at",
        "stale_after_days",
        "validation_passed",
        "validation_details",
        "summary_facts",
        "summary_text",
        "details",
        "error_message",
        "duration_seconds",
    ]:
        assert column in sql

    assert "REFERENCES compass.sync_runs" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_sync_run_categories_run_id" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_sync_run_categories_category_started" in sql
    assert "idx_pathfinder_content_active_type" in pathfinder_sql


def test_category_sync_result_marks_changed_and_serializes_facts():
    from compass_data_sync.sync_contracts import CategorySyncResult

    result = CategorySyncResult.from_counts(
        category="navigator_answers_citations",
        source_system="tcd",
        rows_updated=12,
        rows_deleted=3,
        row_count_after=19823,
        summary_text="Navigator answers changed",
    )

    assert result.status == "changed"
    assert result.summary_facts["rows_changed"] == 15
    assert result.as_db_record()["summary_facts"]["row_count_after"] == 19823


def test_category_sync_result_marks_unchanged_when_counts_do_not_change():
    from compass_data_sync.sync_contracts import CategorySyncResult

    result = CategorySyncResult.from_counts(
        category="navigator_sources",
        source_system="tcd",
        rows_unchanged=7435,
        row_count_after=7435,
    )

    assert result.status == "unchanged"
    assert result.summary_facts["rows_changed"] == 0


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


class _FakeRunRepositoryConn:
    def __init__(self):
        self.fetchval_calls = []
        self.execute_calls = []

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        return 42

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_sync_run_repository_records_parent_and_category_rows():
    from compass_data_sync.sync_contracts import CategorySyncResult
    from compass_data_sync.sync_run_repository import SyncRunRepository

    conn = _FakeRunRepositoryConn()
    repo = SyncRunRepository(_FakePool(conn))
    run_id = await repo.start_parent_run(scope="daily", triggered_by="manual")

    await repo.record_category(
        run_id,
        CategorySyncResult.from_counts(
            category="publications",
            source_system="airtable",
            rows_inserted=1,
            rows_updated=2,
            row_count_after=747,
            summary_text="Publications changed",
        ),
    )
    await repo.complete_parent_run(
        run_id,
        status="completed",
        summary_facts={"categories": 1},
        summary_text="Daily check completed",
        completed_with_warnings=False,
    )

    assert run_id == 42
    assert "INSERT INTO compass.sync_runs" in conn.fetchval_calls[0][0]
    category_sql, category_args = conn.execute_calls[0]
    assert "INSERT INTO compass.sync_run_categories" in category_sql
    assert category_args[0] == 42
    assert category_args[1] == "publications"
    assert category_args[3] == "changed"
    complete_sql, complete_args = conn.execute_calls[-1]
    assert "summary_facts" in complete_sql
    assert "duration_seconds" in complete_sql
    assert complete_args[0] == 42


@pytest.mark.asyncio
async def test_publications_dry_run_does_not_create_sync_run(monkeypatch):
    from compass_data_sync import sync_publications_airtable

    started = False

    class ExplodingLogger:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            nonlocal started
            started = True
            raise AssertionError("dry-run should not start SyncRunLogger")

    class FakePool:
        async def close(self):
            pass

    async def fake_create_pool(*args, **kwargs):
        return FakePool()

    async def fake_fetch_all_records(client):
        return [{"fields": {"Title": "Report", "URL": "https://example.org/report"}}]

    async def fake_retire_missing(pool, schema, current_ids, dry_run):
        assert dry_run is True
        return 0

    monkeypatch.setenv("AIRTABLE_PAT", "pat_test")
    monkeypatch.setattr(sync_publications_airtable, "SyncRunLogger", ExplodingLogger)
    monkeypatch.setattr(sync_publications_airtable.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(sync_publications_airtable, "fetch_all_records", fake_fetch_all_records)
    monkeypatch.setattr(sync_publications_airtable, "retire_missing", fake_retire_missing)
    monkeypatch.setattr(
        sync_publications_airtable,
        "build_dsn",
        lambda cli_dsn: "postgres://example",
    )
    monkeypatch.setattr(
        sync_publications_airtable.argparse.ArgumentParser,
        "parse_args",
        lambda self: SimpleNamespace(
            all=False,
            dry_run=True,
            concurrency=1,
            dsn="postgres://example",
            skip_retirement=False,
            schema="compass",
        ),
    )

    await sync_publications_airtable.main()

    assert started is False


@pytest.mark.asyncio
async def test_nces_recent_import_returns_not_due(monkeypatch):
    from compass_data_sync import nces_import

    class Conn:
        async def fetchrow(self, sql):
            return {
                "row_count_after": 19530,
                "database_freshness_at": datetime.now(UTC) - timedelta(days=2),
            }

    result = await nces_import.sync_nces(
        pool=_FakePool(Conn()),
        client=None,
        dry_run=False,
        force_refresh=False,
        stale_after_days=30,
    )

    assert result.status == "not_due"
    assert result.category == "nces_national_data"
    assert result.row_count_after == 19530


@pytest.mark.asyncio
async def test_daily_sync_records_categories_and_marks_partial_on_failure():
    from compass_data_sync import daily_sync
    from compass_data_sync.sync_contracts import CategorySyncResult

    class FakeRepository:
        def __init__(self):
            self.categories = []
            self.completed = None

        async def start_parent_run(self, scope, triggered_by):
            return 99

        async def record_category(self, run_id, result):
            self.categories.append((run_id, result))

        async def complete_parent_run(
            self, run_id, status, summary_facts, summary_text, completed_with_warnings
        ):
            self.completed = (run_id, status, summary_facts, summary_text, completed_with_warnings)

        async def fail_parent_run(self, run_id, error_message, summary_facts, summary_text):
            raise AssertionError("category failures should not fail the parent run")

    async def ok_adapter(context):
        return CategorySyncResult.from_counts(
            category=context.category,
            source_system="fake",
            rows_unchanged=1,
            row_count_after=1,
        )

    async def failing_adapter(context):
        raise RuntimeError("source unavailable")

    repo = FakeRepository()
    adapters = {
        "navigator_districts_topics": ok_adapter,
        "navigator_answers_citations": ok_adapter,
        "navigator_sources": failing_adapter,
        "pathfinder_guidance": ok_adapter,
        "publications": ok_adapter,
        "nces_national_data": ok_adapter,
        "materialized_views": ok_adapter,
        "validation": ok_adapter,
    }

    results = await daily_sync.run_daily_sync(
        pool=object(),
        repository=repo,
        adapters=adapters,
        selected_categories=list(adapters),
        dry_run=False,
        triggered_by="manual",
    )

    assert len(results) == 8
    assert len(repo.categories) == 8
    assert all(result.completed_at is not None for _, result in repo.categories)
    assert repo.completed[1] == "partial"
    assert repo.completed[4] is True


@pytest.mark.asyncio
async def test_daily_sync_dry_run_writes_no_audit_rows():
    from compass_data_sync import daily_sync
    from compass_data_sync.sync_contracts import CategorySyncResult

    class ExplodingRepository:
        async def start_parent_run(self, scope, triggered_by):
            raise AssertionError("dry-run should not start a parent run")

    async def ok_adapter(context):
        return CategorySyncResult.from_counts(
            category=context.category,
            source_system="fake",
            rows_unchanged=1,
        )

    results = await daily_sync.run_daily_sync(
        pool=object(),
        repository=ExplodingRepository(),
        adapters={"publications": ok_adapter},
        selected_categories=["publications"],
        dry_run=True,
    )

    assert len(results) == 1
    assert results[0].category == "publications"


def test_daily_runner_sources_env_conditionally():
    runner = Path("src/compass_data_sync/run_daily_sync.sh").read_text()

    assert "if [ -f .env ]; then" in runner
    assert "source .env" in runner


def test_daily_sync_fail_on_partial_exit_code_follows_category_attention():
    from compass_data_sync import daily_sync
    from compass_data_sync.sync_contracts import CategorySyncResult

    ok = CategorySyncResult.from_counts(
        category="publications",
        source_system="airtable",
        rows_unchanged=1,
    )
    failed = CategorySyncResult.failed(
        category="navigator_answers_citations",
        source_system="tcd",
        error_message="rate limited",
    )

    assert daily_sync._exit_code_for_results([ok], fail_on_partial=True) == 0
    assert daily_sync._exit_code_for_results([failed], fail_on_partial=True) == 1
    assert daily_sync._exit_code_for_results([failed], fail_on_partial=False) == 0


def test_daily_sync_cli_declares_fail_on_partial_option():
    script = Path("src/compass_data_sync/daily_sync.py").read_text()

    assert "--fail-on-partial" in script


@pytest.mark.asyncio
async def test_pathfinder_guidance_sync_reads_wordpress_and_writes_cache():
    from compass_data_sync import sync_pathfinder_guidance

    class FakeResponse:
        def __init__(self, record):
            self.record = record
            self.headers = {"x-wp-total": "1", "x-wp-totalpages": "1"}

        def raise_for_status(self):
            return None

        def json(self):
            return [self.record]

    class FakeClient:
        async def get(self, url, params, headers):
            assert "CompassDataSync" in headers["User-Agent"]
            route = url.rsplit("/", 1)[-1]
            record_type = {"tcd-topic": "tcd-topic", "pages": "page", "article": "article"}[route]
            return FakeResponse(
                {
                    "id": len(route),
                    "type": record_type,
                    "slug": f"{record_type}-slug",
                    "date_gmt": "2026-05-01T00:00:00",
                    "modified_gmt": "2026-05-12T00:00:00",
                    "link": f"https://www.nctq.org/{record_type}/",
                    "title": {"rendered": record_type},
                    "acf": {},
                }
            )

    class Conn:
        def __init__(self):
            self.executemany_calls = []

        async def fetch(self, sql):
            return []

        async def fetchval(self, sql):
            return 3

        async def executemany(self, sql, rows):
            self.executemany_calls.append((sql, rows))

    conn = Conn()
    result = await sync_pathfinder_guidance.sync_pathfinder_guidance(
        pool=_FakePool(conn),
        client=FakeClient(),
        dry_run=False,
    )

    assert result.category == "pathfinder_guidance"
    assert result.source_system == "pathfinder_wordpress"
    assert result.rows_inserted == 3
    assert result.row_count_after == 3
    assert "Pathfinder WordPress API content" in result.summary_text
    assert len(conn.executemany_calls[0][1]) == 3


def test_data_universe_source_groups_are_visible_sources_not_internal_checks():
    from nctqai.routes.compass import data_universe

    rendered_groups = str(data_universe.SOURCE_RESULT_GROUPS)

    assert "Pathfinder Guidance" in rendered_groups
    assert "materialized_views" not in rendered_groups
    assert "validation" not in rendered_groups
