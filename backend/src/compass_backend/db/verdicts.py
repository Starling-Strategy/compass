"""Write-capable repo for compass.verdicts rows.

Spec B-spine PR 1 — write_one is used by both the live L2 path (PR 6) and the
offline CLI (PR 7). Read methods are used by the Scorecard service (PR 8).
"""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg

from compass_backend.config import Settings, settings
from compass_backend.db.rows import VerdictRecord

logger = logging.getLogger(__name__)

# Issue #1122: over the Tailscale tunnel to staging Postgres, asyncpg (no
# pre-ping) can hand out a NAT-reclaimed dead connection, or a reconnect's SSL
# handshake can time out under concurrent sweep load — observed ~32x in a K=5
# Selection sweep, downgrading it to ``partial``. These are connection-*setup*
# failures that surface from ``pool.acquire()`` before any SQL is sent, so
# re-acquiring a fresh connection is safe and cannot double-write a verdict.
_ACQUIRE_RETRY_ERRORS = (
    asyncpg.exceptions.ConnectionDoesNotExistError,
    asyncpg.exceptions.InterfaceError,
    ConnectionError,
    TimeoutError,
    OSError,
)
_ACQUIRE_MAX_ATTEMPTS = 3


def _verdict_from_row(row: asyncpg.Record) -> VerdictRecord:
    evidence = row["evidence"]
    if isinstance(evidence, str):
        evidence = json.loads(evidence)
    # outcome_bucket is populated by the migration-074 trigger. Older rows
    # written before the trigger landed will have it populated by the
    # one-shot backfill in the same migration. The column may still be
    # absent if the read happens against a pre-074 schema in a test fixture
    # — fall back to None and let consumers route through bucket_verdict.
    outcome_bucket: str | None
    try:
        outcome_bucket = row["outcome_bucket"]
    except (KeyError, IndexError):
        outcome_bucket = None
    return VerdictRecord(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        turn_index=row["turn_index"],
        step_index=row["step_index"],
        case_id=row["case_id"],
        scenario_id=row["scenario_id"],
        criterion_id=row["criterion_id"],
        criterion_version_hash=row["criterion_version_hash"],
        criterion_set_version=row["criterion_set_version"],
        judge_source=row["judge_source"],
        scope=row["scope"],
        references_turn_indices=list(row["references_turn_indices"] or []),
        outcome=row["outcome"],
        reason=row["reason"],
        evidence=evidence,
        trace_id=row["trace_id"],
        triggered_by=row["triggered_by"],
        sweep_run_id=str(row["sweep_run_id"]) if row["sweep_run_id"] else None,
        outcome_bucket=outcome_bucket,
    )


class VerdictsRepository:
    """Async write+read repo for compass.verdicts."""

    def __init__(self, pool: asyncpg.Pool, source: Settings = settings):
        self._pool = pool
        self._schema = source.pg_schema

    async def _acquire_with_retry(self) -> tuple[object, asyncpg.Connection]:
        """Acquire a pooled connection, retrying connection-setup failures.

        Issue #1122: over Tailscale, ``pool.acquire()`` can raise a connect
        timeout / ``ConnectionDoesNotExistError`` when asyncpg opens a fresh
        connection to replace a NAT-reclaimed idle one. These failures happen
        *before any SQL is sent*, so re-acquiring is safe — it cannot
        double-write a verdict. We retry only the acquire; the INSERT runs
        once, on the healthy connection this returns.

        Returns the ``(acquire_context, connection)`` pair so the caller can
        run the INSERT on ``connection`` and release it via the context's
        ``__aexit__``.
        """
        last_exc: BaseException | None = None
        for attempt in range(1, _ACQUIRE_MAX_ATTEMPTS + 1):
            try:
                ctx = self._pool.acquire()
                conn = await ctx.__aenter__()
                return ctx, conn
            except _ACQUIRE_RETRY_ERRORS as exc:
                last_exc = exc
                if attempt >= _ACQUIRE_MAX_ATTEMPTS:
                    break
                backoff = 0.5 * (2 ** (attempt - 1))
                logger.warning(
                    "verdicts.write_one: connection acquire failed "
                    "(attempt %d/%d), retrying in %.1fs: %s",
                    attempt,
                    _ACQUIRE_MAX_ATTEMPTS,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
        assert last_exc is not None  # only reached after a retry-eligible failure
        raise last_exc

    async def write_one(self, verdict: VerdictRecord) -> str:
        """Insert a single verdict; return the assigned UUID.

        Issue #1122: the verdict-write phase of a sweep over Tailscale hits
        connection-setup failures (NAT-reclaimed dead connection / connect
        timeout). ``_acquire_with_retry`` re-attempts only the acquire with
        backoff so a single stale connection doesn't abort the write — the
        INSERT itself runs exactly once, on a connection the acquire has
        already proven healthy, so there is no double-write risk.
        """
        ctx, conn = await self._acquire_with_retry()
        try:
            new_id = await conn.fetchval(
                f"""
                INSERT INTO {self._schema}.verdicts (
                    session_id, turn_index, step_index, case_id, scenario_id,
                    criterion_id, criterion_version_hash, criterion_set_version,
                    judge_source, scope, references_turn_indices,
                    outcome, reason, evidence, trace_id,
                    triggered_by, sweep_run_id
                ) VALUES (
                    $1::uuid, $2, $3, $4, $5,
                    $6, $7, $8,
                    $9, $10, $11::integer[],
                    $12, $13, $14::jsonb, $15,
                    $16, $17::uuid
                )
                RETURNING id
                """,
                verdict.session_id, verdict.turn_index, verdict.step_index,
                verdict.case_id, verdict.scenario_id,
                verdict.criterion_id, verdict.criterion_version_hash,
                verdict.criterion_set_version,
                verdict.judge_source, verdict.scope,
                verdict.references_turn_indices,
                verdict.outcome, verdict.reason,
                verdict.evidence.model_dump_json(), verdict.trace_id,
                verdict.triggered_by, verdict.sweep_run_id,
            )
        finally:
            await ctx.__aexit__(None, None, None)
        return str(new_id)

    async def read_for_session(self, session_id: str) -> list[VerdictRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM {self._schema}.verdicts "
                f"WHERE session_id = $1::uuid AND scope = 'turn' "
                f"ORDER BY turn_index, criterion_id",
                session_id,
            )
        return [_verdict_from_row(r) for r in rows]

    async def read_one(self, verdict_id: str) -> VerdictRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {self._schema}.verdicts WHERE id = $1::uuid",
                verdict_id,
            )
        return _verdict_from_row(row) if row else None


__all__ = ["VerdictsRepository"]
