"""W0-02: verdict_writer.write_one must surface persistence failures.

When the underlying ``VerdictsRepository.write_one`` raises, the L2 chat
contract still requires ``write_one`` to swallow the exception and return
``None`` so the chat pipeline is never blocked on a DB outage. Before
W0-02 the failure was lost in a log line and the verdict simply vanished;
the sweep reported ``completed`` while data was silently dropped.

Post-W0-02:
  * ``write_one`` must call ``_write_persistence_failure_marker`` which
    writes a typed ``VerdictRecord(outcome="error",
    reason="persistence_failed: <ExceptionType>")`` row so the failure
    lands in the existing ``bucket_verdict`` taxonomy
    (``scripts/pa_eval/outcome_taxonomy.py``) as a real ``error`` bucket.
  * ``write_one`` must still return ``None`` for the caller so
    fire-and-forget callers do not change behaviour.
  * The persistence-failure ContextVar counter must be incremented so
    pa-eval's runner can downgrade ``sweep_status`` from ``completed``
    to ``partial`` (separate test, ``scripts/pa_eval/tests/test_runner_status_honest.py``).

Run:
    PYTHONPATH=src uv run pytest \\
      src/compass_backend/tests/test_verdict_writer_records_persistence_failure.py \\
      -x --tb=short
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from compass_backend.db.rows import VerdictRecord
from compass_backend.tests._evidence_fixtures import default_evidence_dict
from compass_backend.quality import verdict_writer
from compass_backend.quality.criteria import COMPASS_CRITERIA_SET_VERSION


def _sample_verdict(
    *,
    outcome: str = "pass",
    reason: str = "ok",
    criterion_id: int = 42,
    sweep_run_id: str | None = None,
) -> VerdictRecord:
    """Build a syntactically valid VerdictRecord for repo round-trip tests."""
    return VerdictRecord(
        session_id=str(uuid.uuid4()),
        turn_index=0,
        step_index=0,
        case_id=7,
        scenario_id=3,
        criterion_id=criterion_id,
        criterion_version_hash="hash-test-v1",
        criterion_set_version=COMPASS_CRITERIA_SET_VERSION,
        judge_source="deterministic",
        scope="turn",
        outcome=outcome,  # type: ignore[arg-type]
        reason=reason,
        evidence=default_evidence_dict(observed="data"),
        trace_id="trace-test",
        triggered_by="sweep_run",
        sweep_run_id=sweep_run_id or str(uuid.uuid4()),
    )


class _AlwaysRaisesRepo:
    """Stub that mirrors VerdictsRepository's write_one signature but raises."""

    def __init__(self, pool: Any, *, exception: Exception) -> None:
        self._pool = pool
        self._exception = exception
        self.write_one_calls = 0

    async def write_one(self, verdict: VerdictRecord) -> str:
        self.write_one_calls += 1
        raise self._exception


class _OriginalRaisesThenMarkerSucceedsRepo:
    """First write_one raises (original verdict), second succeeds (marker)."""

    def __init__(self, pool: Any, *, exception: Exception) -> None:
        self._pool = pool
        self._exception = exception
        self.writes: list[VerdictRecord] = []

    async def write_one(self, verdict: VerdictRecord) -> str:
        self.writes.append(verdict)
        if len(self.writes) == 1:
            raise self._exception
        return "marker-" + uuid.uuid4().hex[:8]


@pytest.mark.asyncio
async def test_write_one_returns_none_when_repo_raises(monkeypatch) -> None:
    """L2 non-blocking semantic: write_one must never let the exception escape.

    Even with the new marker-row write inside, the public return type for
    failure must still be ``None`` so fire-and-forget callers are unchanged.
    """
    verdict = _sample_verdict()
    original_exc = RuntimeError("primary DB write blew up")

    repos: list[_OriginalRaisesThenMarkerSucceedsRepo] = []

    def _factory(pool: Any, source=None) -> _OriginalRaisesThenMarkerSucceedsRepo:
        repo = _OriginalRaisesThenMarkerSucceedsRepo(pool, exception=original_exc)
        repos.append(repo)
        return repo

    monkeypatch.setattr(verdict_writer, "VerdictsRepository", _factory)

    result = await verdict_writer.write_one(verdict, pool=object())

    assert result is None, (
        "L2 chat-non-blocking semantic broken: write_one must return None on "
        "persistence failure even when the marker write succeeds."
    )


@pytest.mark.asyncio
async def test_write_one_persists_typed_error_marker_on_failure(monkeypatch) -> None:
    """W0-02: a typed VerdictRecord(outcome='error', reason='persistence_failed: ...')
    must be written via the same repository so the failure lands in
    compass.verdicts under the bucket_verdict 'error' bucket.
    """
    original_verdict = _sample_verdict(
        outcome="pass",
        reason="catalog adjudication matched expected metric",
        sweep_run_id="sweep-w0-02-test",
    )
    original_exc = ConnectionResetError("pool peer reset")

    repos: list[_OriginalRaisesThenMarkerSucceedsRepo] = []

    def _factory(pool: Any, source=None) -> _OriginalRaisesThenMarkerSucceedsRepo:
        repo = _OriginalRaisesThenMarkerSucceedsRepo(pool, exception=original_exc)
        repos.append(repo)
        return repo

    monkeypatch.setattr(verdict_writer, "VerdictsRepository", _factory)

    result = await verdict_writer.write_one(original_verdict, pool=object())

    assert result is None
    # Two repository writes happened: the failing original + the marker.
    assert len(repos) >= 1, "write_one never instantiated the repository"
    all_writes = [w for repo in repos for w in repo.writes]
    assert len(all_writes) == 2, (
        f"expected 2 writes (failed original + marker), saw {len(all_writes)}"
    )

    marker = all_writes[1]
    assert marker.outcome == "error", (
        f"marker outcome must be 'error', was {marker.outcome!r}; the "
        f"bucket_verdict taxonomy depends on this exact value."
    )
    assert marker.reason.startswith("persistence_failed:"), (
        f"marker reason must announce persistence failure, was {marker.reason!r}"
    )
    assert "ConnectionResetError" in marker.reason, (
        "marker reason must include the original ExceptionType so operators "
        "can grep compass.verdicts by failure mode"
    )
    # Identity fields must be carried forward so the marker is attributable
    # to the dropped verdict's criterion + sweep run.
    assert marker.criterion_id == original_verdict.criterion_id
    assert marker.session_id == original_verdict.session_id
    assert marker.sweep_run_id == original_verdict.sweep_run_id
    assert marker.scope == original_verdict.scope
    assert marker.triggered_by == original_verdict.triggered_by


@pytest.mark.asyncio
async def test_write_one_increments_persistence_failure_counter(monkeypatch) -> None:
    """W0-02: each failed write must bump the ContextVar so the runner can
    downgrade sweep_status to 'partial'.
    """
    verdict = _sample_verdict()

    def _factory(pool: Any, source=None) -> _AlwaysRaisesRepo:
        return _AlwaysRaisesRepo(pool, exception=RuntimeError("boom"))

    monkeypatch.setattr(verdict_writer, "VerdictsRepository", _factory)

    counter = verdict_writer.persistence_failure_counter_reset()
    assert counter == [0]
    assert verdict_writer.persistence_failure_count() == 0

    await verdict_writer.write_one(verdict, pool=object())
    await verdict_writer.write_one(verdict, pool=object())

    assert verdict_writer.persistence_failure_count() == 2, (
        "persistence_failure_count must increment once per failed write_one; "
        "this counter is the signal pa-eval reads to flip sweep_status."
    )


@pytest.mark.asyncio
async def test_write_one_does_not_raise_when_marker_also_fails(monkeypatch) -> None:
    """Defense in depth: if BOTH the original write and the marker write
    raise, write_one must STILL return None (chat pipeline survives) and
    the counter must STILL increment (operator sees the lost row).
    """
    verdict = _sample_verdict()
    repo = _AlwaysRaisesRepo(object(), exception=RuntimeError("DB is fully gone"))

    monkeypatch.setattr(verdict_writer, "VerdictsRepository", lambda pool, source=None: repo)
    verdict_writer.persistence_failure_counter_reset()

    result = await verdict_writer.write_one(verdict, pool=object())

    assert result is None
    assert verdict_writer.persistence_failure_count() == 1
    # Both attempts ran — original + marker.
    assert repo.write_one_calls == 2


@pytest.mark.asyncio
async def test_write_one_returns_uuid_on_success(monkeypatch) -> None:
    """Happy path regression: success path must keep returning the new id."""
    verdict = _sample_verdict()
    expected_id = "verdict-uuid-success-path"

    class _OkRepo:
        def __init__(self, pool: Any, source=None) -> None:
            self._pool = pool

        async def write_one(self, v: VerdictRecord) -> str:
            return expected_id

    monkeypatch.setattr(verdict_writer, "VerdictsRepository", _OkRepo)
    verdict_writer.persistence_failure_counter_reset()

    result = await verdict_writer.write_one(verdict, pool=object())

    assert result == expected_id
    assert verdict_writer.persistence_failure_count() == 0, (
        "happy path must not touch the persistence-failure counter"
    )
