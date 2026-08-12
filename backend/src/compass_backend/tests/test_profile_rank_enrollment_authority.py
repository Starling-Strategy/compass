"""Issue #1270 / #1022 / #1219: ranking covered districts by the *enrollment*
profile field must honor the supervisory-union roll-up authority, just like
``select_largest_districts``.

"The largest districts by enrollment" is normalized into the profile-field
ranking path (``planning/profile_fields.py`` drops the ``largest_districts``
scope and rewrites it to a presentation sort over the ``enrollment`` profile
field). That path read the raw ``navigator_districts.enrollment`` value and
silently bypassed the ``district_enrollment_authority`` override, so NYC ranked
on its ~10k geographic sub-district figure and fell out of the top 10. These
tests pin the COALESCE into the generated SQL for enrollment, and assert it is
*not* applied to other governed rank fields (e.g. FRPL %), which have no such
override."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from compass_backend.config import Settings
from compass_backend.db import ReadOnlyCatalogRepository
from compass_backend.tests._chat_pool_fixtures import FakeChatPool, FakeConn


def _settings() -> Settings:
    return Settings(pg_schema="compass", pg_password=SecretStr("test-password"))


def _conn() -> FakeConn:
    # Rows must carry the columns the rank result-builder reads.
    return FakeConn(
        {
            "navigator_districts": [
                {
                    "district_id": 99,
                    "district_name": "NYC",
                    "state": "NY",
                    "value": 847030.0,
                    "release_yr": 2023,
                }
            ]
        }
    )


@pytest.mark.asyncio
async def test_rank_by_enrollment_coalesces_supervisory_union_authority() -> None:
    connection = _conn()
    repo = ReadOnlyCatalogRepository(_settings(), pool=FakeChatPool(conn=connection))
    await repo.rank_districts_by_profile_field(
        "enrollment", limit=10, academic_year="2024 - 2025"
    )

    sql, _ = connection.queries[0]
    assert "district_enrollment_authority" in sql
    # The override must be COALESCE'd onto the raw navigator value, scoped to
    # the roll-up method — not merely joined.
    assert "COALESCE(auth.enrollment" in sql
    assert "supervisory_union_rollup" in sql


@pytest.mark.asyncio
async def test_rank_by_non_enrollment_field_has_no_authority_override() -> None:
    connection = _conn()
    repo = ReadOnlyCatalogRepository(_settings(), pool=FakeChatPool(conn=connection))
    await repo.rank_districts_by_profile_field(
        "frpl_pct", limit=10, academic_year="2024 - 2025"
    )

    sql, _ = connection.queries[0]
    # FRPL % has no supervisory-union override; the authority table must not
    # leak into the ranking expression for non-enrollment fields.
    assert "district_enrollment_authority" not in sql
    assert "supervisory_union_rollup" not in sql
