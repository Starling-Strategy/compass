"""Thin wrapper over the Logfire query HTTP API.

The Control Tower reads from Logfire for span trees, rule fire counts, and
latency distributions. All functions here return empty results on any failure
so the dashboard stays usable when Logfire is unreachable.

Notes on the Logfire query API (verified against logfire-us.pydantic.dev,
2026-04-30):
  - `records.duration` is `DOUBLE PRECISION` in **seconds**. Multiply by
    1000 to get milliseconds.
  - `trace_id` is a top-level column on `records`, not a JSON attribute.
  - `/v1/query` returns column-oriented JSON (`{"columns": [{"name",
    "values": []}]}`) regardless of the `row_oriented=true` query param.
    We pivot manually below.
  - `/v1/query` truncates at 100 rows even without an explicit `LIMIT`. A
    typical Compass turn emits ~150 spans, so callers needing the full
    tree must paginate or narrow the WHERE.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

LOGFIRE_BASE = os.environ.get("LOGFIRE_BASE_URL", "https://logfire-us.pydantic.dev")
LOGFIRE_READ_TOKEN = os.environ.get("LOGFIRE_READ_TOKEN") or os.environ.get("LOGFIRE_TOKEN")
USER_AGENT = "compass-dashboard/1.0"


def _pivot(payload: dict) -> list[dict[str, Any]]:
    """Convert Logfire's column-oriented response to a list of row dicts."""
    if "rows" in payload:
        return payload["rows"]
    columns = payload.get("columns") or []
    if not columns:
        return []
    names = [c.get("name") for c in columns]
    values = [c.get("values") or [] for c in columns]
    n = max((len(v) for v in values), default=0)
    return [
        {names[j]: values[j][i] if i < len(values[j]) else None for j in range(len(names))}
        for i in range(n)
    ]


async def query(sql: str, *, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Run a Logfire SQL query. Returns [] on any error."""
    if not LOGFIRE_READ_TOKEN:
        logger.debug("LOGFIRE_READ_TOKEN unset — returning empty result")
        return []

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{LOGFIRE_BASE}/v1/query",
                params={"sql": sql, "row_oriented": "true"},
                headers={
                    "Authorization": f"Bearer {LOGFIRE_READ_TOKEN}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            resp.raise_for_status()
            return _pivot(resp.json())
    except Exception:
        logger.exception("Logfire query failed: %s", sql[:120])
        return []


async def fetch_span_tree(trace_id: str) -> list[dict[str, Any]]:
    """Return all spans for a given trace_id ordered by start_timestamp.

    Caps at 100 rows (Logfire's API limit). For long traces this drops the
    tail — pagination is a follow-up.
    """
    if not trace_id:
        return []
    sql = f"""
        SELECT span_name,
               duration * 1000.0 as duration_ms,
               start_timestamp,
               attributes,
               parent_span_id,
               span_id
        FROM records
        WHERE trace_id = '{trace_id.replace("'", "''")}'
        ORDER BY start_timestamp ASC
        LIMIT 100
    """
    return await query(sql)
