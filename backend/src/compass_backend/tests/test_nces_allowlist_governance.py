"""W2-M5-00a (#863): governed NCES allowlist + profile rank fields.

The plan's §7 W2-M5-00a entry calls out three governance leaks in
``src/compass_backend/db/catalog.py``:

- ``_NCES_FIELDS`` (line 51) carries the catalog of NCES profile fields
  Compass exposes (enrollment, FRPL, urbanicity, fiscal) as an in-code
  tuple with no paired governance migration. This is a prereq for
  W2-M5-03 (#747): NCES citations need a governed URL allowlist.
- ``_PROFILE_RANK_FIELDS`` (line 170) maps a public ``field_key`` to the
  rank-by-profile-field surface used by ``rank_districts_by_profile_field``.
  Currently an unknown key silently returns ``[]`` (line 957), which the
  executor treats as ``no rows`` rather than ``unsupported field``.
- The empty-list path on unknown ``field_key`` masks an unsupported-shape
  case as a deterministic-no-data case, hiding bugs and preventing the
  executor from emitting a useful clarification.

These tests pin the new governed surface: typed allowlist + typed rank
fields + typed ``UnsupportedRankField`` exception. The pattern mirrors
migrations 033 (district normalization) and 034 (peer scoring policy).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

NCES_MIGRATION_PATH = Path(
    "src/compass_data_sync/migrations/060_compass_nces_allowlist.sql"
)
PROFILE_RANK_MIGRATION_PATH = Path(
    "src/compass_data_sync/migrations/061_compass_profile_rank_fields.sql"
)


# --------------------------------------------------------------------------- #
# NCES allowlist — schema + sync                                              #
# --------------------------------------------------------------------------- #


def test_nces_allowlist_migration_declares_governed_schema() -> None:
    """Migration 060 must declare ``compass.nces_allowlist`` with audit cols.

    Schema constraints mirror migrations 033 and 034 (review_status check +
    active default + unique index on the natural key). Without these, the
    allowlist drifts silently and ``Citation.title`` rendering can't pull a
    stable canonical URL.
    """

    sql = NCES_MIGRATION_PATH.read_text()
    assert "CREATE TABLE IF NOT EXISTS compass.nces_allowlist" in sql
    assert "field_key TEXT NOT NULL UNIQUE" in sql
    assert "label TEXT NOT NULL" in sql
    assert "data_type TEXT NOT NULL" in sql
    assert "canonical_url TEXT NOT NULL" in sql
    assert "review_status TEXT NOT NULL DEFAULT 'approved'" in sql
    assert "active BOOLEAN NOT NULL DEFAULT TRUE" in sql
    assert "nces_allowlist_review_status_check" in sql


def _values_block(sql: str, table: str) -> str:
    """Return only the ``VALUES (...)`` portion of an INSERT for ``table``.

    Slicing avoids regex false positives on enum literals in check constraints
    (e.g. ``IN ('approved', 'candidate', 'rejected')``).
    """

    insert_marker = f"INSERT INTO compass.{table}"
    start = sql.index(insert_marker)
    values_start = sql.index("VALUES", start) + len("VALUES")
    end = sql.index("ON CONFLICT", values_start)
    return sql[values_start:end]


def test_nces_allowlist_in_sync_with_migration() -> None:
    """In-code ``DEFAULT_NCES_ALLOWLIST`` must mirror migration 060 seed rows.

    The drift check is what keeps the in-code surface authoritative-but-paired:
    operators can edit either side, CI catches them out of sync.
    """

    from compass_backend.catalog.nces_allowlist import DEFAULT_NCES_ALLOWLIST

    sql = NCES_MIGRATION_PATH.read_text()
    values_sql = _values_block(sql, "nces_allowlist")
    in_sql_keys = set(re.findall(r"\(\s*'([a-z_]+)'\s*,", values_sql))
    in_code_keys = {entry.field_key for entry in DEFAULT_NCES_ALLOWLIST}
    assert in_code_keys, "DEFAULT_NCES_ALLOWLIST must not be empty"
    assert in_code_keys == in_sql_keys, (
        f"NCES allowlist drift: in_code={sorted(in_code_keys)} "
        f"in_sql={sorted(in_sql_keys)}"
    )


def test_nces_allowlist_every_entry_has_canonical_url() -> None:
    """Every governed NCES field must carry a canonical_url for citation use.

    W2-M5-03 (#747) renders NCES citations by pulling ``canonical_url`` per
    field; a missing URL would silently regress to the pre-PR behavior of
    no Sources column entry.
    """

    from compass_backend.catalog.nces_allowlist import DEFAULT_NCES_ALLOWLIST

    for entry in DEFAULT_NCES_ALLOWLIST:
        assert entry.canonical_url.startswith("https://"), (
            f"NCES allowlist entry {entry.field_key!r} missing https:// "
            f"canonical_url (got {entry.canonical_url!r})"
        )


def test_nces_allowlist_includes_core_m5_fields() -> None:
    """The known M5-blocking fields (enrollment, FRPL, fiscal) must be present."""

    from compass_backend.catalog.nces_allowlist import DEFAULT_NCES_ALLOWLIST

    keys = {entry.field_key for entry in DEFAULT_NCES_ALLOWLIST}
    for required in ("enrollment", "frpl_pct", "pupil_teacher_ratio", "locale_text"):
        assert required in keys, (
            f"missing core NCES field {required!r} — would block W2-M5-03 (#747)"
        )


# --------------------------------------------------------------------------- #
# Profile rank fields — schema + sync                                         #
# --------------------------------------------------------------------------- #


def test_profile_rank_fields_migration_declares_governed_schema() -> None:
    """Migration 061 must declare ``compass.profile_rank_fields`` with audit cols."""

    sql = PROFILE_RANK_MIGRATION_PATH.read_text()
    assert "CREATE TABLE IF NOT EXISTS compass.profile_rank_fields" in sql
    assert "field_key TEXT NOT NULL UNIQUE" in sql
    assert "column_name TEXT NOT NULL" in sql
    assert "label TEXT NOT NULL" in sql
    assert "metric_id INTEGER NOT NULL" in sql
    assert "display TEXT NOT NULL" in sql
    assert "vintage_label TEXT NOT NULL" in sql
    assert "review_status TEXT NOT NULL DEFAULT 'approved'" in sql
    assert "active BOOLEAN NOT NULL DEFAULT TRUE" in sql


def test_profile_rank_fields_in_sync_with_migration() -> None:
    """In-code ``DEFAULT_PROFILE_RANK_FIELDS`` must mirror migration 061 seed."""

    from compass_backend.catalog.profile_rank_fields import (
        DEFAULT_PROFILE_RANK_FIELDS,
    )

    sql = PROFILE_RANK_MIGRATION_PATH.read_text()
    values_sql = _values_block(sql, "profile_rank_fields")
    in_sql_keys = set(re.findall(r"\(\s*'([a-z_]+)'\s*,", values_sql))
    in_code_keys = {field.field_key for field in DEFAULT_PROFILE_RANK_FIELDS}
    assert in_code_keys, "DEFAULT_PROFILE_RANK_FIELDS must not be empty"
    assert in_code_keys == in_sql_keys, (
        f"profile_rank_fields drift: in_code={sorted(in_code_keys)} "
        f"in_sql={sorted(in_sql_keys)}"
    )


def test_profile_rank_fields_includes_enrollment_and_frpl() -> None:
    """Both rank fields currently in-code at db/catalog.py:170 must survive."""

    from compass_backend.catalog.profile_rank_fields import (
        DEFAULT_PROFILE_RANK_FIELDS,
    )

    keys = {field.field_key for field in DEFAULT_PROFILE_RANK_FIELDS}
    assert keys >= {"enrollment", "frpl_pct"}


# --------------------------------------------------------------------------- #
# UnsupportedRankField — typed blocker replaces the silent empty list         #
# --------------------------------------------------------------------------- #


def test_unsupported_rank_field_is_typed_blocker() -> None:
    """An unknown ``field_key`` must surface as a typed exception, not ``[]``.

    Today ``db/catalog.py:957`` is::

        if spec is None:
            return []

    This collapses two distinct cases into one: a real empty result and an
    unsupported-shape request. The plan's W2-M5-00a entry pins the typed
    surface: ``Unsupported rank field returns typed UnsupportedRankField
    blocker (not empty list)``.
    """

    from compass_backend.catalog.profile_rank_fields import (
        UnsupportedRankField,
    )

    err = UnsupportedRankField(field_key="totally_made_up_field")
    assert err.field_key == "totally_made_up_field"
    assert isinstance(err, Exception)


@pytest.mark.asyncio
async def test_rank_districts_by_profile_field_raises_for_unknown_key() -> None:
    """The repository must raise ``UnsupportedRankField`` (not return ``[]``).

    This is the contract change executors and tests depend on: an unknown
    ``field_key`` is a planner/catalog wiring bug, not a data shortage.
    Silently returning an empty list hides the bug and produces a confusing
    ``no covered districts`` rendering.
    """

    from compass_backend.config import settings
    from compass_backend.db._pool import ChatPoolHolder
    from compass_backend.db.catalog import ReadOnlyCatalogRepository
    from compass_backend.catalog.profile_rank_fields import (
        UnsupportedRankField,
    )

    # ``UnsupportedRankField`` fires before any DB call, so an empty
    # holder is fine; the pool is required only by the constructor.
    repo = ReadOnlyCatalogRepository(source=settings, pool=ChatPoolHolder())

    with pytest.raises(UnsupportedRankField) as excinfo:
        await repo.rank_districts_by_profile_field(
            "this_field_does_not_exist",
            academic_year="2024 - 2025",
        )

    assert excinfo.value.field_key == "this_field_does_not_exist"


@pytest.mark.asyncio
async def test_rank_districts_by_profile_field_executor_emits_refusal() -> None:
    """An ``UnsupportedRankField`` raised below the executor must surface as
    ``ExecutionRefusal``, not crash the chat route.

    This pins the catalog→execution boundary: the typed blocker propagates
    up to ``execution/operations.py`` and becomes a typed refusal the
    renderer can format as a useful clarification.
    """

    from compass_backend.execution.executor import DeterministicQueryExecutor
    from compass_backend.execution.types import ExecutionRefusal

    # Build a sort_step with an unsupported field_key; the executor must
    # convert the catalog-layer UnsupportedRankField into a typed refusal.
    class _FakeCatalog:
        async def rank_districts_by_profile_field(self, field_key, **_kwargs):
            from compass_backend.catalog.profile_rank_fields import (
                UnsupportedRankField,
            )

            raise UnsupportedRankField(field_key=field_key)

    executor = DeterministicQueryExecutor.__new__(DeterministicQueryExecutor)
    executor._catalog = _FakeCatalog()
    executor._current_academic_year = "2024 - 2025"

    from compass_backend.contracts.planning import (
        MetricSpec,
        QueryPlan,
        SelectionSpec,
        SortStepSpec,
    )

    plan = QueryPlan(
        operation="rank",
        question="Rank by enrollmnt_typo, largest first.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="placeholder")],
        sort_steps=[
            SortStepSpec(field="enrollmnt_typo", direction="desc"),
        ],
    )

    rows_or_refusal = await executor._rank_profile_rows(
        plan,
        plan.sort_steps[0],
        limit=10,
    )
    assert isinstance(rows_or_refusal, ExecutionRefusal)
    assert "enrollmnt_typo" in rows_or_refusal.message
