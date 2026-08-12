"""SQL query helpers for the Compass Quality Scorecard.

Used by ``compass_backend.quality.scorecard`` (backend pure functions) which
are in turn called by ``nctqai.services.compass_quality.loaders``.

No ORM — plain asyncpg queries against compass.* tables. All helpers are
module-level async functions that accept a pool; no class wrapper needed
for the scorecard read path (read-only, no shared state).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import asyncpg

from compass_backend.config import settings


# ── Internal dataclasses (not exposed to the dashboard) ──────────────────


@dataclass
class TrialRollup:
    """One verdict row that is relevant to a case trial."""
    session_id: str
    outcome: str          # "pass" | "fail" | "error"
    judge_source: str     # "deterministic" | "judge_prompt" | "span_assertion" | "inline_deterministic"
    criterion_code: str
    reason: str | None


@dataclass
class CaseRollup:
    """All verdict rows for one (scenario_id, case_id, session_id) triple.

    ``case_inputs`` is the raw JSONB ``compass.cases.inputs`` payload
    (typically ``[{"prompt": "...", "step_complete_when": ...}, ...]``).
    Used by ``compute_dimension_detail`` to derive a human display name
    for the case (so the UI shows the prompt text instead of just the
    bare ``case_code``).
    """
    scenario_id: int
    case_id: int
    scenario_code: str
    case_code: str
    session_id: str
    trials: list[TrialRollup] = field(default_factory=list)
    case_inputs: list | None = None


@dataclass(frozen=True)
class SweepRunSummary:
    """Completed sweep-run metadata used by scorecard trend computation."""

    id: str
    dimension: str
    finished_at: str | None = None
    verdict_count: int | None = None
    trial_count: int | None = None
    case_count: int | None = None
    k: int | None = None
    git_sha: str | None = None
    # Grader-provenance fingerprint (migration 091, issue #1257). Nullable so
    # pre-091 rows read None; _sweep_runs_are_comparable treats None as "not
    # comparable" — the safe default that suppresses a delta across an unknown
    # grader rather than inventing one.
    criterion_set_version: str | None = None
    criterion_version_hashes: str | None = None
    check_types: list[str] | None = None


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_ident(value: str) -> str:
    if not _IDENT_RE.fullmatch(value):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return f'"{value}"'


# ── SQL helpers ───────────────────────────────────────────────────────────


async def _resolve_latest_completed_sweep_run_id(
    conn: asyncpg.Connection,
    *,
    schema: str,
    dimension: str,
) -> str | None:
    """Return the most-comprehensive completed sweep_run id for *dimension*.

    Ordering: ``case_count DESC NULLS LAST, finished_at DESC NULLS LAST``.

    Rationale (audit finding #3d, 2026-05-26): the earlier rule was
    "latest finished_at," which let a 3-case post-audit smoke shadow a
    75-case validating nightly that finished earlier the same morning.
    The dashboard then surfaced a noise-level scorecard. By preferring
    the sweep with the most distinct cases tested (``case_count`` is
    populated by ``_record_sweep_end``), the dashboard always reads the
    richest snapshot we ran for that dimension. Ties — and rows where
    ``case_count`` is NULL (pre-W0-02 sweeps that didn't populate it) —
    fall back to the recency rule.

    W0-02 invariant preserved: the rollup is still pinned to exactly one
    ``sweep_run_id`` per call. ``status = 'completed'`` is still required;
    ``partial`` sweeps stay out of the dashboard. Returns ``None`` when
    no eligible sweep exists so the caller can surface "no data yet."
    """
    row = await conn.fetchrow(
        f"""
        SELECT id::text AS id
        FROM {schema}.sweep_runs
        WHERE dimension = $1
          AND status = 'completed'
        ORDER BY case_count DESC NULLS LAST,
                 finished_at DESC NULLS LAST
        LIMIT 1
        """,
        dimension,
    )
    if row is None:
        return None
    return row["id"]


async def load_case_rollups_for_dimension(
    pool: asyncpg.Pool,
    dimension: str,
    *,
    sweep_run_id: str | None = None,
) -> list[CaseRollup]:
    """Return all verdict rows grouped into CaseRollup objects for *dimension*.

    Args:
        pool: asyncpg connection pool.
        dimension: Human-readable dimension name, e.g. "Sort Accuracy".
                   Matches ``compass.scenarios.accuracy_primary_dimension``
                   and ``compass.sweep_runs.dimension``.
        sweep_run_id: Optional UUID to restrict verdicts to a specific sweep
                      run. ``None`` (default) automatically resolves to the
                      latest ``status='completed'`` sweep for this dimension
                      in ``compass.sweep_runs`` via
                      :func:`_resolve_latest_completed_sweep_run_id` so the
                      result is pinned to exactly one sweep run and cannot
                      silently mix verdicts across runs (W0-02). Sweeps that
                      ended ``partial`` or ``failed`` are intentionally
                      excluded.

    Returns:
        List of CaseRollup objects, one per (scenario_id, case_id, session_id)
        combination that has at least one verdict row.
    """
    schema = settings.pg_schema

    async with pool.acquire() as conn:
        # W0-02 sweep-pinning: resolve the chosen sweep_run_id first so the
        # rollup query reads exactly one sweep run. Skips the rollup query
        # entirely when there is no eligible completed sweep — that's the
        # honest "no readings yet" answer.
        pinned_sweep_run_id = (
            sweep_run_id
            if sweep_run_id
            else await _resolve_latest_completed_sweep_run_id(
                conn,
                schema=schema,
                dimension=dimension,
            )
        )
        if pinned_sweep_run_id is None:
            return []

        sql = f"""
            SELECT
                s.id            AS scenario_id,
                s.scenario_code,
                c.id            AS case_id,
                c.case_code,
                c.inputs        AS case_inputs,
                v.session_id    ::text AS session_id,
                cr.criterion_code,
                v.outcome,
                v.judge_source,
                v.reason
            FROM {schema}.verdicts v
            JOIN {schema}.scenarios s  ON s.id = v.scenario_id
            JOIN {schema}.cases c      ON c.id = v.case_id
            JOIN {schema}.criteria cr  ON cr.id = v.criterion_id
            WHERE s.accuracy_primary_dimension = $1
              AND v.scope IN ('turn', 'scenario_fit')
              AND v.triggered_by = 'sweep_run'
              AND v.sweep_run_id = $2::uuid
            ORDER BY s.id, c.id, v.session_id, cr.criterion_code
        """

        rows = await conn.fetch(sql, dimension, pinned_sweep_run_id)

    # Group into CaseRollup by (scenario_id, case_id, session_id)
    rollups: dict[tuple[int, int, str], CaseRollup] = {}
    for row in rows:
        key = (row["scenario_id"], row["case_id"], row["session_id"])
        if key not in rollups:
            inputs_raw = row["case_inputs"]
            if isinstance(inputs_raw, str):
                try:
                    inputs_raw = json.loads(inputs_raw)
                except (ValueError, TypeError):
                    inputs_raw = None
            rollups[key] = CaseRollup(
                scenario_id=row["scenario_id"],
                case_id=row["case_id"],
                scenario_code=row["scenario_code"],
                case_code=row["case_code"],
                session_id=row["session_id"],
                case_inputs=inputs_raw if isinstance(inputs_raw, list) else None,
            )
        rollups[key].trials.append(
            TrialRollup(
                session_id=row["session_id"],
                outcome=row["outcome"],
                judge_source=row["judge_source"],
                criterion_code=row["criterion_code"],
                reason=row["reason"],
            )
        )
    return list(rollups.values())


async def load_completed_sweep_runs_for_dimension(
    pool: asyncpg.Pool,
    dimension: str,
    *,
    limit: int = 2,
    pg_schema: str | None = None,
) -> list[SweepRunSummary]:
    """Return latest completed sweep runs for one human-readable dimension."""
    schema = _quote_ident(pg_schema or settings.pg_schema)
    # criterion_set_version / criterion_version_hashes / check_types come from
    # migration 091 (issue #1257). _sweep_runs_are_comparable uses them to prove
    # two runs shared the same grader before exposing a delta.
    sql = f"""
        SELECT
            id::text AS id,
            dimension,
            finished_at::text AS finished_at,
            verdict_count,
            trial_count,
            case_count,
            k,
            git_sha,
            criterion_set_version,
            criterion_version_hashes,
            check_types
        FROM {schema}.sweep_runs
        WHERE dimension = $1
          AND status = 'completed'
        ORDER BY finished_at DESC NULLS LAST
        LIMIT $2
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, dimension, limit)

    return [
        SweepRunSummary(
            id=row["id"],
            dimension=row["dimension"],
            finished_at=row["finished_at"],
            verdict_count=row["verdict_count"],
            trial_count=row["trial_count"],
            case_count=row["case_count"],
            k=row["k"],
            git_sha=row["git_sha"],
            criterion_set_version=row["criterion_set_version"],
            criterion_version_hashes=row["criterion_version_hashes"],
            check_types=list(row["check_types"])
            if row["check_types"] is not None
            else None,
        )
        for row in rows
    ]


async def load_conversation_turns(
    pool: asyncpg.Pool,
    session_id: str,
) -> list[dict]:
    """Return user/assistant turn pairs for a session, oldest first.

    Filters out legacy pipeline-marker rows (``[resolve_metric]``,
    ``Final result processed.``) that polluted history before commit 6694cb0.

    Turn numbering is 1-based: the verdict writer (verdict_pipeline) stamps
    ``turn_index = session.turn_count`` — the count *after* the turn saved,
    so the first turn is 1 — and the reader must match that ledger basing
    for verdicts to attach to the right turn.

    Returns:
        List of dicts with keys: turn_index (1-based), user_text,
        assistant_text.
    """
    schema = settings.pg_schema

    sql = f"""
        SELECT role, content
        FROM {schema}.chat_messages
        WHERE session_id = $1
          AND content NOT LIKE '[%'
          AND content != 'Final result processed.'
          AND length(content) > 5
        ORDER BY timestamp, id
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, session_id)

    # Pair up user/assistant messages into turn dicts (1-based, see docstring)
    turns = []
    pending_user: str | None = None
    turn_index = 1
    for row in rows:
        if row["role"] == "user":
            pending_user = row["content"]
        elif row["role"] == "assistant" and pending_user is not None:
            turns.append({
                "turn_index": turn_index,
                "user_text": pending_user,
                "assistant_text": row["content"],
            })
            turn_index += 1
            pending_user = None
    return turns


async def load_verdicts_for_session_grouped_by_turn(
    pool: asyncpg.Pool,
    session_id: str,
) -> dict[int, list[dict]]:
    """Return verdicts keyed by turn_index for a session.

    Returns:
        Dict of turn_index → list of dicts with keys:
        criterion_code, judge_source, outcome, reason.
    """
    schema = settings.pg_schema

    sql = f"""
        SELECT
            v.turn_index,
            cr.criterion_code,
            v.judge_source,
            v.outcome,
            v.reason
        FROM {schema}.verdicts v
        JOIN {schema}.criteria cr ON cr.id = v.criterion_id
        WHERE v.session_id = $1::uuid
          AND v.scope = 'turn'
        ORDER BY v.turn_index, cr.criterion_code
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, session_id)

    grouped: dict[int, list[dict]] = {}
    for row in rows:
        ti = row["turn_index"]
        if ti not in grouped:
            grouped[ti] = []
        grouped[ti].append({
            "criterion_code": row["criterion_code"],
            "judge_source": row["judge_source"],
            "outcome": row["outcome"],
            "reason": row["reason"],
        })
    return grouped


async def load_session_scenario_context(
    pool: asyncpg.Pool,
    session_id: str,
) -> dict | None:
    """Return scenario_id + display name for a session's earliest turn verdict.

    Sessions launched by the sweep runner (PR 7) have ``compass.verdicts``
    rows whose ``scenario_id`` ties the conversation back to the originating
    ``compass.scenarios`` row. Organic L1-only chats (no verdicts) get
    ``None`` — that's the honest answer.

    Returns:
        ``{"scenario_id": int, "scenario_name": str}`` when at least one
        verdict exists and its scenario row resolves, else ``None``.
        ``compass.scenarios`` has no dedicated title column today, so
        ``scenario_name`` is the ``scenario_code`` (e.g. ``"G14"``).

    Perf: ``scope = 'turn'`` and resolving the scenario_id in an isolated
    subquery (instead of ``ORDER BY v.id`` across a join) are load-bearing —
    they let the planner use ``idx_verdicts_session_turn`` (~0.7ms) instead of
    scanning the whole 415k-row ``verdicts`` table (~3.2s, worse under load).
    ``v.id`` is a UUID, so the old ``ORDER BY v.id`` was an arbitrary order
    anyway; ``turn_index`` order is both meaningful (earliest turn) and indexed.
    """
    schema = settings.pg_schema

    sql = f"""
        SELECT s.id AS scenario_id, s.scenario_code
        FROM {schema}.scenarios s
        JOIN (
            SELECT scenario_id
            FROM {schema}.verdicts
            WHERE session_id = $1::uuid
              AND scope = 'turn'
              AND scenario_id IS NOT NULL
            ORDER BY turn_index
            LIMIT 1
        ) v ON s.id = v.scenario_id
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, session_id)
    if not row:
        return None
    return {
        "scenario_id": row["scenario_id"],
        "scenario_name": row["scenario_code"],
    }


async def load_session_metadata(
    pool: asyncpg.Pool,
    session_id: str,
) -> dict | None:
    """Return basic metadata for a session (session_id, created_at, trace_id).

    Returns None if the session does not exist.
    """
    schema = settings.pg_schema

    sql = f"""
        SELECT
            s.session_id ::text,
            s.created_at,
            m.trace_id
        FROM {schema}.chat_sessions s
        LEFT JOIN {schema}.message_feedback m ON m.session_id = s.session_id
        WHERE s.session_id = $1
        LIMIT 1
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, session_id)
    if not row:
        return None
    return {
        "session_id": row["session_id"],
        "created_at": str(row["created_at"]),
        "trace_id": row["trace_id"],
    }


__all__ = [
    "CaseRollup",
    "SweepRunSummary",
    "TrialRollup",
    "load_completed_sweep_runs_for_dimension",
    "load_case_rollups_for_dimension",
    "load_conversation_turns",
    "load_verdicts_for_session_grouped_by_turn",
    "load_session_metadata",
    "load_session_scenario_context",
]
