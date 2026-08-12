"""Regression: the largest-by-enrollment composition must not silently drop
a covered district that has valid enrollment authority.

Issues #1022 (Fairfax missing from enrollment + salary scenarios) and #1219
(asking which big districts pay the most leaves out one of the biggest)
share a root cause in how the "largest districts" set is composed at the
catalog boundary. The live staging repro is an upstream DATA defect (NYC's
``navigator_nces_link`` row points at NCES geographic sub-district LEAID
``3600076`` instead of the citywide DOE, so NYC's enrollment is recorded as
~10k instead of ~900k; that drop cascades so Fairfax appears excluded from a
"10 largest" it should belong to). That data fix is proposed in the issue
thread, not forced as a code change.

This test locks the one *code* correctness guarantee that the composition
must hold regardless of upstream data shape: ``select_largest_districts``
must rank by each district's best-available enrollment authority and must
NOT drop a district merely because it lacks a row for the exact requested
``ay_range``. The pre-fix SQL inner-joined a ``current_districts`` CTE gated
on ``ay_range = $1`` against the enrollment authority, so a district present
only in a prior directory snapshot (no current-year row) was silently
excluded from the "largest" set even with valid enrollment. The directory
snapshot lagging a year would then silently shrink the largest set — exactly
the silent-drop failure mode scenario SORT-MIGRATED-283 forbids.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from compass_backend.config import Settings
from compass_backend.db import ReadOnlyCatalogRepository
from compass_backend.tests._chat_pool_fixtures import FakeChatPool, FakeConn


def _settings() -> Settings:
    return Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
    )


def _pool_for(connection: FakeConn) -> FakeChatPool:
    return FakeChatPool(conn=connection)


@pytest.mark.asyncio
async def test_select_largest_districts_does_not_inner_join_on_current_year() -> None:
    """The largest-district SQL must not gate the result on a current-year row.

    A covered district with enrollment authority but no row for the exact
    requested ``ay_range`` must still be eligible for the "largest" set. The
    fix sources district identity from the full roster and LEFT JOINs the
    enrollment authority, so the ``ay_range = $1`` filter prefers — but does
    not require — a current-year directory row.
    """

    connection = FakeConn(
        {
            "navigator_districts": [
                {
                    "district_id": 99,
                    "district_name": "New York City Department of Education",
                    "state": "NY",
                },
                {
                    "district_id": 175,
                    "district_name": "Fairfax County Public Schools",
                    "state": "VA",
                },
            ]
        }
    )
    repository = ReadOnlyCatalogRepository(_settings(), pool=_pool_for(connection))

    districts = await repository.select_largest_districts(
        limit=10,
        academic_year="2024 - 2025",
    )

    assert [district.district_id for district in districts] == [99, 175]

    sql, args = connection.queries[0]
    assert 'FROM "compass"."navigator_districts"' in sql
    assert "enrollment DESC NULLS LAST" in sql
    # Enrollment authority must be LEFT JOINed so a district without a
    # current-year directory row (but with prior enrollment) is retained,
    # not silently dropped by an INNER JOIN.
    assert "LEFT JOIN enrollment_authority" in sql
    # District identity is sourced from the full roster with the requested
    # year preferred via ordering, rather than a hard ``WHERE ay_range = $1``
    # gate on the identity CTE that would exclude prior-only districts.
    assert "WHERE ay_range = $1" not in sql
    assert "(ay_range = $1) DESC" in sql
    assert args == ("2024 - 2025", [], 10)
