"""Service boundary for guarded Compass answer rewriting."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Protocol

from compass_backend.agents.model_settings import agent_model_settings
from compass_backend.answer_layer.agent import build_answer_stylist_agent
from compass_backend.answer_layer.briefs import build_answer_brief
from compass_backend.answer_layer.validation import validate_answer_draft
from compass_backend.contracts.answer_layer import (
    AnswerDraft,
    AnswerLayerFinding,
    AnswerLayerMode,
    AnswerLayerReport,
)
from compass_backend.artifacts import ResultSet
from compass_backend.contracts.rendering import ResponseManifest

_logger = logging.getLogger(__name__)


class AnswerStylistAgent(Protocol):
    async def run(self, prompt: str): ...


def allowed_answer_layer_result_types(
    value: str | Iterable[str] | None,
) -> set[str]:
    """Normalize answer-layer result-type settings into a lookup set."""

    if value is None:
        return set()
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = value
    return {part.strip() for part in parts if part and part.strip()}


async def improve_answer(
    manifest: ResponseManifest,
    *,
    mode: AnswerLayerMode,
    allowed_result_types: str | Iterable[str],
    route: str = "execute",
    result_set: ResultSet | None = None,
    allowed_nctq_context: Iterable[str] = (),
    stylist_agent: AnswerStylistAgent | None = None,
) -> tuple[str, AnswerLayerReport]:
    """Improve a rendered answer when allowed, returning body plus safety report."""

    deterministic_body = manifest.body
    disabled = _disabled_report(mode)
    if mode == "off":
        return deterministic_body, disabled.model_copy(update={"reason": "mode_off"})
    if manifest.status != "rendered":
        return deterministic_body, disabled.model_copy(
            update={"reason": "manifest_not_rendered"}
        )
    if manifest.result_type not in allowed_answer_layer_result_types(
        allowed_result_types
    ):
        return deterministic_body, disabled.model_copy(
            update={"reason": "result_type_not_allowed"}
        )

    try:
        brief = build_answer_brief(
            manifest,
            route=route,
            result_set=result_set,
            allowed_nctq_context=allowed_nctq_context,
        )
        agent = stylist_agent or build_answer_stylist_agent()
        result = await asyncio.wait_for(
            agent.run(brief.model_dump_json(indent=2)),
            timeout=agent_model_settings.answer_stylist_timeout_seconds,
        )
        draft = result.output
        if not isinstance(draft, AnswerDraft):
            draft = AnswerDraft.model_validate(draft)
        report = validate_answer_draft(brief, draft, mode=mode)
    except asyncio.TimeoutError:
        _logger.warning("answer layer timeout: stylist exceeded %.1fs ceiling", agent_model_settings.answer_stylist_timeout_seconds)
        return deterministic_body, AnswerLayerReport(
            mode=mode,
            attempted=True,
            accepted=False,
            status="fallback",
            reason="answer_layer_timeout",
            findings=(
                AnswerLayerFinding(
                    code="answer_layer_timeout",
                    message=f"Answer layer timed out after {agent_model_settings.answer_stylist_timeout_seconds}s.",
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("answer layer fallback: %s", type(exc).__name__)
        return deterministic_body, AnswerLayerReport(
            mode=mode,
            attempted=True,
            accepted=False,
            status="fallback",
            reason="answer_layer_exception",
            findings=(
                AnswerLayerFinding(
                    code="answer_layer_exception",
                    message=f"Answer layer raised {type(exc).__name__}.",
                ),
            ),
        )

    if mode == "shadow":
        return deterministic_body, report.model_copy(
            update={"accepted": False, "status": "shadow_generated"}
        )
    if report.accepted:
        return draft.body, report
    return deterministic_body, report


def _disabled_report(mode: AnswerLayerMode) -> AnswerLayerReport:
    return AnswerLayerReport(
        mode=mode,
        attempted=False,
        accepted=False,
        status="disabled",
    )


__all__ = [
    "AnswerStylistAgent",
    "allowed_answer_layer_result_types",
    "improve_answer",
]
