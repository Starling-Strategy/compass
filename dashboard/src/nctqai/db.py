"""Database helpers for the NCTQ.ai dashboard.

Uses psycopg2 connection pool for efficient connection reuse.
All queries go through psycopg2 with RealDictCursor for dict-style rows.
"""

import logging
import os

import psycopg2
import psycopg2.extras
import psycopg2.pool

from nctqai.config import Config

logger = logging.getLogger(__name__)

# Schema names — single source of truth for the dashboard. Independent of
# Compass backend settings so the dashboard container does not need any backend
# DB env vars. Default values match the
# staging/production layout where the dashboard reads `compass.*`, `silver.*`,
# and `bronze.*`.
COMPASS_SCHEMA = os.getenv("NCTQAI_COMPASS_SCHEMA", "compass")
SILVER_SCHEMA = os.getenv("NCTQAI_SILVER_SCHEMA", "silver")
BRONZE_SCHEMA = os.getenv("NCTQAI_BRONZE_SCHEMA", "bronze")

_pool = None

# Cached answer to "does compass.chat_messages.snapshot_summary exist?" (migration
# 163). None = not yet probed; True/False = definitive. See
# snapshot_summary_column_available for the caching rationale.
_snapshot_summary_column_available: bool | None = None

# Migration 208 adds the v2 artifact keys and the named refresh procedure. A
# v1 summary has no CSV/chart fields, so it must not be mistaken for an honest
# zero before the backfill has completed.
_artifact_summary_v2_available: bool | None = None


def _get_pool():
    global _pool
    if _pool is None:
        cfg = Config()
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            host=cfg.pg_host,
            port=cfg.pg_port,
            database=cfg.pg_database,
            user=cfg.pg_user,
            password=cfg.pg_password.get_secret_value(),
        )
    return _pool


def run_sql(sql: str, params: tuple = ()) -> list[dict]:
    """Execute a SELECT query and return a list of dicts.

    Always rolls back the implicit transaction before returning the connection
    to the pool. psycopg2 starts a transaction on the first cursor.execute(),
    and putconn() does NOT auto-rollback — without this, an aborted query
    (or even a clean SELECT that errored mid-fetch) returns the connection
    in an "idle in transaction" state, poisoning the next user.

    If the rollback itself raises (e.g. server-side termination, dropped
    socket), we mark the connection for discard so the pool doesn't hand
    out a broken socket to the next caller.
    """
    pool = _get_pool()
    conn = pool.getconn()
    discard = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(row) for row in cur.fetchall()]
        conn.rollback()
        return rows
    except Exception:
        try:
            conn.rollback()
        except Exception:
            discard = True
        raise
    finally:
        pool.putconn(conn, close=discard)


def snapshot_summary_column_available() -> bool:
    """Return True when ``compass.chat_messages.snapshot_summary`` exists.

    That column is added by migration 163. Several dashboard reads probe it (the
    Overview "What people ask" aggregate, the Conversations has-table
    filter/count, and per-conversation snapshot memory). On an environment where
    the dashboard image was deployed ahead of migration 163, those reads raise
    ``UndefinedColumn`` and 500 the page. This capability check lets each read
    site fall back to a safe, empty/false result instead.

    The definitive present/absent answer is cached for the process lifetime
    (schema does not change under a running process in the normal
    apply-migrations-then-deploy order). A probe error is NOT cached — it is
    treated as absent for this one call and re-checked next time — so a transient
    DB blip cannot pin the dashboard into permanent degradation.
    """
    global _snapshot_summary_column_available
    if _snapshot_summary_column_available is not None:
        return _snapshot_summary_column_available
    sql = """
        SELECT COUNT(*) AS has_col
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = 'chat_messages'
          AND column_name = 'snapshot_summary'
    """
    try:
        rows = run_sql(sql, (COMPASS_SCHEMA,))
    except Exception:
        logger.warning(
            "snapshot_summary capability probe failed; treating the column as "
            "absent for this call (migration 163 status unknown)",
            exc_info=True,
        )
        return False
    available = bool(rows and (rows[0].get("has_col") or 0) > 0)
    if not available:
        logger.warning(
            "%s.chat_messages.snapshot_summary is absent (migration 163 "
            "unapplied); snapshot-backed dashboard reads will degrade to "
            "empty/false until it is applied",
            COMPASS_SCHEMA,
        )
    _snapshot_summary_column_available = available
    return available


def reset_snapshot_summary_capability_cache() -> None:
    """Clear cached snapshot-summary capability answers. Test-only seam."""

    global _snapshot_summary_column_available, _artifact_summary_v2_available
    _snapshot_summary_column_available = None
    _artifact_summary_v2_available = None


def artifact_summary_v2_available() -> bool:
    """Return True only after migration 208 and its v2 backfill are complete.

    Migration 163 provides ``snapshot_summary`` but only migration 208 adds the
    CSV/chart keys. The procedure name proves 208 reached Section C; the stale
    check prevents a partially timed-out backfill from quietly undercounting
    artifacts. Apply migrations before starting the dashboard, then restart the
    dashboard after a resumed backfill so this process-lifetime cache refreshes.
    """
    global _artifact_summary_v2_available
    if _artifact_summary_v2_available is not None:
        return _artifact_summary_v2_available
    if not snapshot_summary_column_available():
        return False

    sql = f"""
        SELECT
            to_regprocedure(%s) IS NOT NULL AS has_v2_refresh,
            NOT EXISTS (
                SELECT 1
                FROM {COMPASS_SCHEMA}.chat_messages
                WHERE role = 'assistant'
                  AND snapshot_summary IS NOT NULL
                  AND COALESCE(snapshot_summary ->> 'v', '') <> '2'
            ) AS backfill_complete
    """
    try:
        rows = run_sql(
            sql,
            (f"{COMPASS_SCHEMA}.refresh_chat_message_snapshot_summary_v2(integer)",),
        )
    except Exception:
        logger.warning(
            "artifact-summary v2 capability probe failed; treating artifacts "
            "as unavailable for this call",
            exc_info=True,
        )
        return False

    row = rows[0] if rows else {}
    available = bool(row.get("has_v2_refresh")) and bool(row.get("backfill_complete"))
    if not available:
        logger.warning(
            "artifact summaries are unavailable: migration 208 is missing or "
            "its v2 backfill is incomplete; tables, CSV exports, and charts "
            "will be labelled unavailable instead of shown as zero",
        )
    _artifact_summary_v2_available = available
    return available


def run_sql_write(sql: str, params: tuple = ()) -> int:
    """Execute an INSERT/UPDATE query, commit, and return the row count.

    Same discard-on-rollback-failure guard as run_sql — keeps a broken
    socket out of the pool when the rollback itself fails.
    """
    pool = _get_pool()
    conn = pool.getconn()
    discard = False
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount
    except Exception:
        try:
            conn.rollback()
        except Exception:
            discard = True
        raise
    finally:
        pool.putconn(conn, close=discard)
