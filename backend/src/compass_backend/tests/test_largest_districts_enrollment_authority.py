"""Issue #1270: largest/range selection must rank on the enrollment authority
override (the supervisory-union roll-up), so NYC ranks at its citywide figure
and the same value reaches every reader (no Selection-fix-that-makes-a-
Consistency-bug)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from compass_backend.config import Settings
from compass_backend.db import ReadOnlyCatalogRepository
from compass_backend.tests._chat_pool_fixtures import FakeChatPool, FakeConn


def _settings() -> Settings:
    return Settings(pg_schema="compass", pg_password=SecretStr("test-password"))


@pytest.mark.asyncio
async def test_largest_districts_sql_coalesces_enrollment_authority() -> None:
    connection = FakeConn(
        {"navigator_districts": [{"district_id": 99, "district_name": "NYC", "state": "NY"}]}
    )
    repo = ReadOnlyCatalogRepository(_settings(), pool=FakeChatPool(conn=connection))
    await repo.select_largest_districts(limit=10, academic_year="2024 - 2025")

    sql, _ = connection.queries[0]
    assert "district_enrollment_authority" in sql
    assert "COALESCE(" in sql and "enrollment" in sql
    # Authority overrides TCD enrollment in the ranking expression.
    assert "supervisory_union_rollup" in sql


@pytest.mark.asyncio
async def test_enrollment_range_sql_coalesces_enrollment_authority() -> None:
    connection = FakeConn(
        {"navigator_districts": [{"district_id": 99, "district_name": "NYC", "state": "NY"}]}
    )
    repo = ReadOnlyCatalogRepository(_settings(), pool=FakeChatPool(conn=connection))
    await repo.select_districts_by_enrollment_range(
        min_enrollment=100000, academic_year="2024 - 2025"
    )
    sql, _ = connection.queries[0]
    assert "district_enrollment_authority" in sql
    # The override must be COALESCE'd into the filtered value, scoped to the
    # roll-up method — not merely joined.
    assert "COALESCE(" in sql
    assert "supervisory_union_rollup" in sql
