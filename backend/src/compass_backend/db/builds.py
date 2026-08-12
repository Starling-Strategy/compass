"""SQL helpers for ``compass.builds`` — the build registry.

Introduced in migration 075 to replace the in-process ``_DEFAULT_BUILD_ID``
sentinel that the Scorecard used to validate against. A build is one
(git_sha, criterion_set_version) snapshot the sweep runner can attribute
verdicts to; this module reads the registry so
``compass_backend.quality.scorecard.resolve_build_or_default`` can swap
the sentinel for a DB query without forcing an ORM dependency.

Read-only for now — sweeps that register new builds will land alongside
runner changes in a follow-on PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import asyncpg

from compass_backend.config import settings


@dataclass(frozen=True)
class BuildRecord:
    """One row from ``compass.builds``."""

    build_id: str
    criterion_set_version: str
    git_sha: str | None
    description: str | None
    created_at: datetime
    finished_at: datetime | None


def _record_from_row(row: asyncpg.Record) -> BuildRecord:
    return BuildRecord(
        build_id=row["build_id"],
        criterion_set_version=row["criterion_set_version"],
        git_sha=row["git_sha"],
        description=row["description"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


async def load_build(pool: asyncpg.Pool, build_id: str) -> BuildRecord | None:
    """Return the registry row for ``build_id`` or ``None`` if unregistered."""
    schema = settings.pg_schema
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT build_id, criterion_set_version, git_sha, description, "
            f"created_at, finished_at FROM {schema}.builds WHERE build_id = $1",
            build_id,
        )
    return _record_from_row(row) if row else None


async def load_latest_build(pool: asyncpg.Pool) -> BuildRecord | None:
    """Return the most recently created build, or ``None`` if registry empty."""
    schema = settings.pg_schema
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT build_id, criterion_set_version, git_sha, description, "
            f"created_at, finished_at FROM {schema}.builds "
            f"ORDER BY created_at DESC LIMIT 1"
        )
    return _record_from_row(row) if row else None


__all__ = [
    "BuildRecord",
    "load_build",
    "load_latest_build",
]
