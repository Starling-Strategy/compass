"""Single-row verdict writer — wraps VerdictsRepository with try/swallow.

The L2 path (fire-and-forget post-turn) must never raise: any DB error is
logged and swallowed so the already-returned chat response is unaffected.

W0-02 addition: on persistence failure we still swallow the exception so the
chat pipeline survives, but we ALSO write a typed
``VerdictRecord(outcome="error", reason="persistence_failed: <ExceptionType>")``
marker via ``_write_persistence_failure_marker`` so the failure lands in the
existing ``bucket_verdict`` taxonomy (``scripts/pa_eval/outcome_taxonomy.py``)
as a real ``error``-bucket row instead of being silently lost. Additionally
we increment a contextvar counter so pa-eval's runner can detect that a
persistence failure occurred and downgrade the sweep status from
``completed`` to ``partial``.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from compass_backend.config import Settings, settings
from compass_backend.db.verdicts import VerdictsRepository
from compass_backend.quality.criteria import VerdictRecord

logger = logging.getLogger(__name__)


# Per-task counter for verdict-persistence failures. pa-eval's run_case binds
# a fresh int via ``persistence_failure_counter_reset`` before evaluating a
# trial, then reads it with ``persistence_failure_count`` after the trial to
# learn whether any rows were lost-then-marker'd. Live chat traffic does not
# bind the contextvar; the increment is a harmless no-op there.
_PERSISTENCE_FAILURE_COUNTER: ContextVar[list[int]] = ContextVar(
    "compass_verdict_persistence_failures",
    default=[],
)


def persistence_failure_counter_reset() -> list[int]:
    """Bind a fresh single-slot counter for the current task and return it.

    Caller pattern (pa-eval runner):

        counter = persistence_failure_counter_reset()
        await evaluate_and_write(...)
        if counter[0] > 0:
            ...  # downgrade sweep status to partial

    The counter is a 1-element list because ContextVars hold immutable
    references — we want the inner write_one to mutate a shared cell
    without re-set()ing the var on every increment.
    """
    counter: list[int] = [0]
    _PERSISTENCE_FAILURE_COUNTER.set(counter)
    return counter


def persistence_failure_count() -> int:
    """Read the current persistence-failure count for this task context."""
    counter = _PERSISTENCE_FAILURE_COUNTER.get()
    if not counter:
        return 0
    return counter[0]


def _increment_persistence_failure_counter() -> None:
    counter = _PERSISTENCE_FAILURE_COUNTER.get()
    # When live chat traffic runs the default empty list is shared — never
    # mutate it (would leak across tasks). Only mutate when the runner has
    # bound a fresh single-slot list via ``persistence_failure_counter_reset``.
    if counter:
        counter[0] += 1


async def _write_persistence_failure_marker(
    verdict: VerdictRecord,
    *,
    pool: Any,
    original_exception: BaseException,
    source: Settings = settings,
) -> str | None:
    """Persist a typed ``outcome="error"`` marker row in place of the lost verdict.

    Copies the failed verdict's identifying fields (criterion, scope, session,
    trace, sweep run, etc.) and overrides ``outcome="error"`` plus a
    ``reason="persistence_failed: <ExceptionType>"`` line so the row buckets
    cleanly under ``bucket_verdict`` → ``"error"`` in pa-eval reports.

    Returns the marker UUID on success, or ``None`` if the marker write
    itself fails (in which case the failure is logged but never raised —
    chat pipeline must survive). The original verdict payload is included
    in evidence for forensic debugging.
    """
    try:
        repo = VerdictsRepository(pool, source=source)
        marker = VerdictRecord(
            session_id=verdict.session_id,
            turn_index=verdict.turn_index,
            step_index=verdict.step_index,
            case_id=verdict.case_id,
            scenario_id=verdict.scenario_id,
            criterion_id=verdict.criterion_id,
            criterion_version_hash=verdict.criterion_version_hash,
            criterion_set_version=verdict.criterion_set_version,
            judge_source=verdict.judge_source,
            scope=verdict.scope,
            references_turn_indices=list(verdict.references_turn_indices),
            outcome="error",
            reason=f"persistence_failed: {type(original_exception).__name__}",
            # Preserve the failed verdict's envelope so the marker row carries
            # the same forensic state — operators inspecting compass.verdicts can
            # see what turn the lost verdict was about.
            evidence={
                "artifact_snapshot": verdict.evidence.artifact_snapshot,
                "answer_excerpt": verdict.evidence.answer_excerpt,
                "copied_span_attributes": verdict.evidence.copied_span_attributes,
                "persistence_failure": {
                    "original_outcome": verdict.outcome,
                    "original_reason": verdict.reason,
                    "exception_type": type(original_exception).__name__,
                    "exception_message": str(original_exception),
                },
            },
            trace_id=verdict.trace_id,
            triggered_by=verdict.triggered_by,
            sweep_run_id=verdict.sweep_run_id,
        )
        new_id = await repo.write_one(marker)
        return new_id
    except Exception as marker_exc:
        logger.warning(
            "verdict_writer: failed to persist marker row for criterion_id=%s "
            "session=%s turn=%s — original=%s marker=%s",
            verdict.criterion_id,
            verdict.session_id,
            verdict.turn_index,
            original_exception,
            marker_exc,
            exc_info=True,
        )
        return None


async def write_one(
    verdict: VerdictRecord,
    *,
    pool: Any,  # asyncpg.Pool — typed as Any to keep the dep testable without a live pool
    source: Settings = settings,
) -> str | None:
    """Insert one verdict row; return the assigned UUID or None on failure.

    Errors are logged at WARNING level and swallowed — callers (verdict_pipeline)
    must never block the chat pipeline on persistence failures.

    On persistence failure (W0-02):
        - The contextvar persistence-failure counter is incremented (no-op
          when no counter is bound, e.g. live chat traffic).
        - A typed ``outcome="error"`` marker row is written via
          ``_write_persistence_failure_marker`` so the failure is observable
          in pa-eval reports under the ``error`` bucket of ``bucket_verdict``.
        - The caller still sees ``None`` (preserving L2 chat-non-blocking
          semantics — fire-and-forget continues to ignore the return value).

    Args:
        verdict: Persistence-ready VerdictRecord (criterion_id must be a real
                 FK into compass.criteria — produced by criterion_record_to_evaluator).
        pool:    asyncpg.Pool pointing at the compass schema.

    Returns:
        UUID string on success; None on any error.
    """
    try:
        repo = VerdictsRepository(pool, source=source)
        new_id = await repo.write_one(verdict)
        return new_id
    except Exception as exc:
        logger.warning(
            "verdict_writer: failed to write verdict for criterion_id=%s session=%s turn=%s: %s",
            verdict.criterion_id,
            verdict.session_id,
            verdict.turn_index,
            exc,
            exc_info=True,
        )
        _increment_persistence_failure_counter()
        # Fire-and-forget marker write. We must NEVER let the marker path
        # raise — it is purely diagnostic. The original swallow contract
        # (return None) is preserved for the caller.
        try:
            await _write_persistence_failure_marker(
                verdict,
                pool=pool,
                original_exception=exc,
                source=source,
            )
        except Exception as marker_exc:  # pragma: no cover - defense in depth
            logger.warning(
                "verdict_writer: marker write raised unexpectedly: %s",
                marker_exc,
                exc_info=True,
            )
        return None


__all__ = [
    "write_one",
    "persistence_failure_count",
    "persistence_failure_counter_reset",
]
