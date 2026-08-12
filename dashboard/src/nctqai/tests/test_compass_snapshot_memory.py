"""Regression tests for dashboard reads from Compass turn-snapshot memory."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nctqai.services import compass_conversations, compass_snapshot_memory, compass_stats


def _snapshot(
    *,
    metric_name: str = "Starting salary",
    district_id: int = 1,
    district_name: str = "Denver Public Schools",
    state: str = "CO",
) -> dict[str, Any]:
    district = {
        "district_id": district_id,
        "district_name": district_name,
        "state": state,
    }
    metric = {"metric_id": 89, "metric_name": metric_name}
    return {
        "snapshot_id": "snapshot-1",
        "turn_index": 1,
        "memory": {
            "latest_query_context": {
                "result_districts": [district],
                "result_metrics": [metric],
            },
            "result_refs": [
                {
                    "snapshot_id": "snapshot-1",
                    "turn_index": 1,
                    "question": "What is the starting salary?",
                    "metrics": [metric],
                    "districts": [district],
                    "digest": "1 row for Starting salary including Denver.",
                }
            ],
        },
    }


def _retired_result_rows_has_table(snapshot: dict[str, Any]) -> bool:
    """Independent restatement of the detoast predicate migration 163 replaces.

    The dashboard's retired has-table check was a non-empty ``result.rows`` array
    (SQL: ``jsonb_typeof(result->'rows') = 'array' AND
    jsonb_array_length(result->'rows') > 0``). Migration 163's
    ``compute_chat_message_snapshot_summary`` precomputes exactly that into
    ``snapshot_summary->>'has_table'``. Mirroring the predicate here pins its
    *semantics* in a DB-free unit test. It is deliberately stricter than a bare
    ``bool(rows)``: a truthy non-array (e.g. a JSON object) is False, matching the
    ``jsonb_typeof = 'array'`` guard. This does NOT run the SQL function — that
    conformance must be verified by PR-1's migration smoke test on a scratch DB.
    """
    result = snapshot.get("result")
    rows = result.get("rows") if isinstance(result, dict) else None
    return isinstance(rows, list) and len(rows) > 0


def test_pruned_summary_feeds_python_authority_identically_to_full_blob() -> None:
    """Pins the memory-only contract behind the dashboard's blob -> summary swap.

    This is NOT the migration prune drift guard. The SQL prune lives in migration
    163 (``compass_data_sync``); the thing that must fail if the prune SHAPE drifts
    (``memory`` nested under a wrong key, a dropped field, a string-vs-boolean
    ``has_table``) is PR-1's migration smoke test on a scratch DB plus the staging
    real-data proof (plan section "Tests"). Neither is reachable from this no-DB
    worktree. What this test pins is the *Python half* of the swap:
    ``snapshot_memory_from_payload`` reads ONLY the top-level ``memory`` key, so the
    dashboard gets the same topic_labels/districts whether handed the full
    ``fresh_compass_turn_snapshot`` blob or migration 163's pruned summary --
    provided that summary carries ``memory`` verbatim at the top level.
    """
    full_blob = {
        **_snapshot(),
        # The leg the old _HAS_TABLE_EXISTS detoasted; drives has_table (asserted in
        # test_has_table_mirrors_retired_result_rows_detoast, not here).
        "result": {"rows": [{"district_id": 1, "starting_salary": 50000}]},
    }
    # Migration 163's emitted shape: a top-level ``memory`` carried verbatim plus
    # precomputed scalars. The decoy ``latest_query_context`` is planted as a
    # SIBLING of ``memory`` (the full blob nests that block UNDER memory). The
    # equivalence below therefore only holds if the authority dives through
    # top-level ``memory`` and ignores other top-level keys -- so this catches the
    # AUTHORITY being refactored to read a flattened top-level key. (It does NOT
    # catch the migration's prune shape drifting -- that is PR-1's smoke test.)
    pruned_summary = {
        "v": 1,
        "memory": _snapshot()["memory"],
        "has_table": _retired_result_rows_has_table(full_blob),
        "latest_query_context": {
            "result_districts": [
                {"district_id": 999, "district_name": "Decoy", "state": "ZZ"}
            ]
        },
    }

    from_full = compass_snapshot_memory.snapshot_memory_from_payload(full_blob)
    from_pruned = compass_snapshot_memory.snapshot_memory_from_payload(pruned_summary)

    assert from_full == from_pruned
    assert from_full.topic_labels == from_pruned.topic_labels == ("Starting salary",)
    assert from_full.districts == from_pruned.districts
    assert len(from_pruned.districts) == 1
    # The decoy top-level latest_query_context is ignored: district 999 never leaks in.
    assert all(d.district_id != 999 for d in from_pruned.districts)


def test_has_table_mirrors_retired_result_rows_detoast() -> None:
    """``snapshot_summary->>'has_table'`` semantics: non-empty ``result.rows`` array.

    Migration 163 replaces the dashboard's inline ``result.rows`` detoast with a
    precomputed boolean. This pins the predicate's SEMANTICS against the independent
    restatement above, across its full truth table -- populated, empty, and a
    truthy-non-array payload. The empty and non-array cases are exactly what the
    old ``x == x`` / ``bool([non-empty])`` assertions never exercised: they prove
    the boolean DISCRIMINATES rather than echoing a constant.

    Caveat (cannot be closed from this no-DB worktree): this exercises a Python
    restatement, not ``compute_chat_message_snapshot_summary`` itself, so an
    inverted/broken SQL emit would not fail HERE. That conformance is owned by
    PR-1's migration smoke test on a scratch DB (plan section "Tests").
    """
    populated = {**_snapshot(), "result": {"rows": [{"district_id": 1}]}}
    empty = {**_snapshot(), "result": {"rows": []}}
    non_array = {**_snapshot(), "result": {"rows": {"not": "an array"}}}

    assert _retired_result_rows_has_table(populated) is True
    assert _retired_result_rows_has_table(empty) is False
    # A truthy non-array is False (the SQL jsonb_typeof='array' guard). A naive
    # bool(rows) would wrongly say True here -- this is the divergence the strict
    # mirror exists to catch, and what the old throwaway bool() check missed.
    assert _retired_result_rows_has_table(non_array) is False
    assert bool(non_array["result"]["rows"]) is True


def test_top_question_stats_aggregate_over_snapshot_summary_in_sql(
    monkeypatch,
) -> None:
    """get_top_* slice the in-SQL top-N aggregation over snapshot_summary.

    snapshot_aggregates now GROUPs the precomputed summary in Postgres and returns
    flat (kind, label, did, name, state, c) rows instead of fetching + decoding one
    snapshot per session (which scaled with the window). The three get_top_*
    helpers share one cached aggregation.
    """
    compass_stats._SNAPSHOT_AGGREGATES_CACHE.clear()
    captured_sql: list[str] = []

    def fake_run_sql(sql: str, binds: dict | None = None) -> list[dict[str, Any]]:
        captured_sql.append(sql)
        return [
            {"kind": "topic", "label": "General Salary", "did": None,
             "name": None, "state": None, "c": 2},
            {"kind": "district", "label": None, "did": 1,
             "name": "Denver Public Schools", "state": "CO", "c": 1},
            {"kind": "district", "label": None, "did": 2,
             "name": "Aurora Public Schools", "state": "CO", "c": 1},
            {"kind": "state", "label": "CO", "did": None,
             "name": None, "state": None, "c": 2},
        ]

    monkeypatch.setattr(compass_stats, "run_sql", fake_run_sql)
    # Column-present path (#169): the aggregate reads snapshot_summary.
    monkeypatch.setattr(
        compass_stats, "snapshot_summary_column_available", lambda: True
    )

    assert compass_stats.get_top_topics(limit=3) == [
        {"topic": "General Salary", "session_count": 2}
    ]
    assert compass_stats.get_top_districts(limit=3) == [
        {
            "district_id": 1,
            "district_name": "Denver Public Schools",
            "state": "CO",
            "session_count": 1,
        },
        {
            "district_id": 2,
            "district_name": "Aurora Public Schools",
            "state": "CO",
            "session_count": 1,
        },
    ]
    assert compass_stats.get_top_states(limit=3) == [
        {"state": "CO", "session_count": 2}
    ]
    # The three get_top_* calls share ONE cached aggregation query — not one per call.
    assert len(captured_sql) == 1
    sql = captured_sql[0]
    # It aggregates every compact saved summary in SQL, joins metric IDs to the
    # policy catalog, and never detoasts the full snapshot blob.
    assert "snapshot_summary" in sql
    assert "jsonb_array_elements" in sql
    assert "policy_questions" in sql
    assert "pq.metric_id::text" in sql
    assert "metric_name" not in sql
    assert "WITH turns AS" in sql
    assert "SELECT DISTINCT ON" not in sql
    assert "case_context" not in sql
    # Preserve migration 087's partial-index predicate on the all-turn read.
    assert "message_data ? 'fresh_compass_turn_snapshot'" in sql
    # Contract: each district/state counts once per conversation even if several
    # saved turns carry the same district with differing optional metadata.
    assert "SELECT DISTINCT session_id, did" in sql
    assert "SELECT DISTINCT session_id, dstate" in sql


def test_latest_snapshot_fetch_ranks_lightweight_rows_before_loading_payload(
    monkeypatch,
) -> None:
    captured_sql: list[str] = []

    def fake_run_sql(sql: str, _bind: tuple = ()) -> list[dict[str, Any]]:
        captured_sql.append(sql)
        return []

    monkeypatch.setattr(compass_snapshot_memory, "run_sql", fake_run_sql)
    # Column-present path (#169): the read prefers snapshot_summary.
    monkeypatch.setattr(
        compass_snapshot_memory, "snapshot_summary_column_available", lambda: True
    )

    compass_snapshot_memory.fetch_latest_snapshot_rows()

    [sql] = captured_sql
    assert "WITH ranked AS" in sql
    assert "payload.snapshot_summary" in sql
    assert "SELECT DISTINCT ON (s.session_id)" in sql
    ranked_sql = sql.split("FROM ranked", 1)[0]
    assert "turn_index" not in ranked_sql


def test_snapshot_aggregates_cache_reuses_query(monkeypatch) -> None:
    compass_stats._SNAPSHOT_AGGREGATES_CACHE.clear()
    query_count = 0

    def fake_run_sql(sql: str, binds: dict | None = None) -> list[dict[str, Any]]:
        nonlocal query_count
        query_count += 1
        return [
            {"kind": "topic", "label": "General Salary", "did": None,
             "name": None, "state": None, "c": 1},
        ]

    monkeypatch.setattr(compass_stats, "run_sql", fake_run_sql)
    # Column-present path (#169): the aggregate reads snapshot_summary.
    monkeypatch.setattr(
        compass_stats, "snapshot_summary_column_available", lambda: True
    )

    first = compass_stats.snapshot_aggregates()
    second = compass_stats.snapshot_aggregates()

    assert query_count == 1  # second call served from the TTL cache
    assert first is second
    assert first.topics["General Salary"] == 1


def test_conversation_summary_uses_snapshot_memory_for_primary_district(
    monkeypatch,
) -> None:
    captured_sql: list[str] = []

    def fake_conversation_sql(sql: str, _bind: tuple = ()) -> list[dict[str, Any]]:
        captured_sql.append(sql)
        return [
            {
                "session_id": "s1",
                "created_at": datetime(2026, 5, 28),
                "message_count": 2,
                "first_message": "What is the starting salary?",
                "thumbs_up_count": 0,
                "thumbs_down_count": 0,
            }
        ]

    def fake_snapshot_sql(sql: str, _bind: tuple = ()) -> list[dict[str, Any]]:
        captured_sql.append(sql)
        return [{"session_id": "s1", "snapshot": _snapshot()}]

    monkeypatch.setattr(compass_conversations, "run_sql", fake_conversation_sql)
    monkeypatch.setattr(compass_snapshot_memory, "run_sql", fake_snapshot_sql)

    [summary] = compass_conversations.list_conversations()

    assert summary.district_id == 1
    assert summary.district_name == "Denver Public Schools"
    assert summary.state == "CO"
    assert captured_sql
    assert all("case_context" not in sql for sql in captured_sql)


def test_conversation_detail_does_not_select_or_expose_retired_session_context(
    monkeypatch,
) -> None:
    captured_sql: list[str] = []

    def fake_run_sql(sql: str, _bind: tuple = ()) -> list[dict[str, Any]]:
        captured_sql.append(sql)
        if "FROM compass.chat_sessions" in sql:
            return [{"session_id": "s1", "created_at": datetime(2026, 5, 28)}]
        return []

    monkeypatch.setattr(compass_conversations, "run_sql", fake_run_sql)

    detail = compass_conversations.get_conversation("s1")

    assert detail is not None
    assert not hasattr(detail, "case_context")
    assert captured_sql
    assert all("case_context" not in sql for sql in captured_sql)
