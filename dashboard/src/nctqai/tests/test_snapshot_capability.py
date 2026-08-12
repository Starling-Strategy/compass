"""Migration-ordering graceful degradation for snapshot_summary reads (#169).

Migration 163 adds ``compass.chat_messages.snapshot_summary``. Several dashboard
reads probe it; on an environment where the dashboard image was deployed ahead
of migration 163, those reads would raise ``UndefinedColumn`` and 500 the page.
``snapshot_summary_column_available()`` gates reads that need compact snapshot
memory. Artifact reads additionally require migration 208's completed v2
summary, so missing CSV/chart keys cannot render as a confident zero.

These tests never touch a database: they monkeypatch ``run_sql`` (imported by
name into each service module) to capture the SQL that WOULD run and to return
canned rows, and they monkeypatch the capability check to simulate the
pre-/post-migration environments.
"""

from __future__ import annotations

import nctqai.db as db
from nctqai.services import (
    compass_conversations,
    compass_snapshot_memory,
    compass_stats,
)


# ── Capability probe itself ───────────────────────────────────────────────────


def test_capability_true_when_column_present(monkeypatch) -> None:
    db.reset_snapshot_summary_capability_cache()
    monkeypatch.setattr(db, "run_sql", lambda *a, **k: [{"has_col": 1}])

    assert db.snapshot_summary_column_available() is True


def test_capability_false_when_column_absent_and_caches(monkeypatch) -> None:
    db.reset_snapshot_summary_capability_cache()
    calls: list[int] = []

    def _fake_run_sql(*_a, **_k):
        calls.append(1)
        return [{"has_col": 0}]

    monkeypatch.setattr(db, "run_sql", _fake_run_sql)

    assert db.snapshot_summary_column_available() is False
    # Second call is served from the cache — no second probe.
    assert db.snapshot_summary_column_available() is False
    assert len(calls) == 1


def test_capability_probe_error_returns_false_without_caching(monkeypatch) -> None:
    db.reset_snapshot_summary_capability_cache()

    def _boom(*_a, **_k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(db, "run_sql", _boom)
    assert db.snapshot_summary_column_available() is False

    # A transient error must NOT be cached: a later successful probe wins.
    monkeypatch.setattr(db, "run_sql", lambda *a, **k: [{"has_col": 1}])
    assert db.snapshot_summary_column_available() is True


def test_artifact_v2_capability_requires_refresh_and_completed_backfill(monkeypatch) -> None:
    db.reset_snapshot_summary_capability_cache()
    monkeypatch.setattr(db, "snapshot_summary_column_available", lambda: True)
    monkeypatch.setattr(
        db,
        "run_sql",
        lambda *_a, **_k: [{"has_v2_refresh": True, "backfill_complete": True}],
    )

    assert db.artifact_summary_v2_available() is True


def test_artifact_v2_capability_is_false_for_missing_or_stale_backfill(monkeypatch) -> None:
    db.reset_snapshot_summary_capability_cache()
    monkeypatch.setattr(db, "snapshot_summary_column_available", lambda: True)
    monkeypatch.setattr(
        db,
        "run_sql",
        lambda *_a, **_k: [{"has_v2_refresh": True, "backfill_complete": False}],
    )

    assert db.artifact_summary_v2_available() is False


# ── get_artifact_coverage (compass_stats) ─────────────────────────────────────


def _capture_run_sql(module, rows):
    captured: dict[str, str] = {}

    def _run_sql(sql, params=()):  # noqa: ANN001
        captured["sql"] = sql
        return rows

    return _run_sql, captured


def test_artifact_coverage_marks_counts_unavailable_without_v2_summary(monkeypatch) -> None:
    monkeypatch.setattr(
        compass_stats, "artifact_summary_v2_available", lambda: False
    )
    run_sql, captured = _capture_run_sql(
        compass_stats, [{"sessions": 7, "with_table": 0}]
    )
    monkeypatch.setattr(compass_stats, "run_sql", run_sql)

    coverage = compass_stats.get_artifact_coverage()

    # Honest denominator preserved, but zero artifact counts are explicitly
    # unavailable rather than a claim that the data contains no artifacts.
    assert coverage.sessions == 7
    assert coverage.with_table == 0
    assert coverage.table_pct == 0
    assert coverage.available is False
    # The degraded query must NOT reference the missing column.
    assert "snapshot_summary" not in captured["sql"]


def test_artifact_coverage_uses_v2_summary_when_present(monkeypatch) -> None:
    monkeypatch.setattr(
        compass_stats, "artifact_summary_v2_available", lambda: True
    )
    run_sql, captured = _capture_run_sql(
        compass_stats, [{"sessions": 10, "with_table": 4}]
    )
    monkeypatch.setattr(compass_stats, "run_sql", run_sql)

    coverage = compass_stats.get_artifact_coverage()

    assert coverage.sessions == 10
    assert coverage.with_table == 4
    assert coverage.table_pct == 40
    assert coverage.available is True
    assert "snapshot_summary" in captured["sql"]


# ── snapshot aggregates (compass_stats) ───────────────────────────────────────


def test_snapshot_aggregates_empty_and_skips_query_when_column_absent(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        compass_stats, "snapshot_summary_column_available", lambda: False
    )

    def _must_not_run(*_a, **_k):
        raise AssertionError("run_sql must not be called when the column is absent")

    monkeypatch.setattr(compass_stats, "run_sql", _must_not_run)

    aggregates = compass_stats._query_snapshot_aggregates(None, None)

    assert len(aggregates.topics) == 0
    assert len(aggregates.districts) == 0
    assert aggregates.district_meta == {}
    assert len(aggregates.states) == 0


# ── snapshot memory read (compass_snapshot_memory) ────────────────────────────


def test_snapshot_memory_reads_blob_directly_when_column_absent(monkeypatch) -> None:
    monkeypatch.setattr(
        compass_snapshot_memory, "snapshot_summary_column_available", lambda: False
    )
    run_sql, captured = _capture_run_sql(compass_snapshot_memory, [])
    monkeypatch.setattr(compass_snapshot_memory, "run_sql", run_sql)

    compass_snapshot_memory.fetch_latest_snapshot_rows()

    # Degraded query reads message_data directly and never names the column.
    assert "snapshot_summary" not in captured["sql"]
    assert "message_data" in captured["sql"]


def test_snapshot_memory_prefers_column_when_present(monkeypatch) -> None:
    monkeypatch.setattr(
        compass_snapshot_memory, "snapshot_summary_column_available", lambda: True
    )
    run_sql, captured = _capture_run_sql(compass_snapshot_memory, [])
    monkeypatch.setattr(compass_snapshot_memory, "run_sql", run_sql)

    compass_snapshot_memory.fetch_latest_snapshot_rows()

    assert "snapshot_summary" in captured["sql"]
    assert "COALESCE" in captured["sql"]


# ── Conversations has-table filter/count (compass_conversations) ──────────────


def test_conversations_artifact_tabs_match_nothing_without_v2_summary(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        compass_conversations, "artifact_summary_v2_available", lambda: False
    )
    run_sql, captured = _capture_run_sql(compass_conversations, [])
    monkeypatch.setattr(compass_conversations, "run_sql", run_sql)

    compass_conversations.list_conversations(tab="has-table")

    # The artifact predicate degrades to FALSE, never referencing the column.
    assert "snapshot_summary" not in captured["sql"]


def test_conversations_tab_counts_skip_artifacts_without_v2_summary(
    monkeypatch,
) -> None:
    from datetime import UTC, datetime

    monkeypatch.setattr(
        compass_conversations, "artifact_summary_v2_available", lambda: False
    )
    run_sql, captured = _capture_run_sql(compass_conversations, [{}])
    monkeypatch.setattr(compass_conversations, "run_sql", run_sql)

    # ``since`` is set so artifact counts would normally be computed — the v2
    # capability guard is what suppresses them here.
    compass_conversations.count_conversations_by_tab(
        since=datetime(2026, 6, 1, tzinfo=UTC)
    )

    assert "snapshot_summary" not in captured["sql"]
