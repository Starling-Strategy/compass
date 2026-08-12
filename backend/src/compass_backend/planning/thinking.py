"""Deterministic planner-thinking admission policy for SSN-271."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from compass_backend.agents.model_settings import (
    PlannerThinkingEffort,
    PlannerThinkingPolicy,
)
from compass_backend.reference import normalize_whitespace_casefold

_TARGET_SCENARIO_IDS = {
    "3",
    "37",
    "78",
    "83",
    "106",
    "107",
    "148",
    "158",
    "160",
    "179",
    "182",
    "184",
    "191",
    "197",
    "207",
    "209",
    "239",
    "250",
    "253",
    "270",
}
_FOLLOWUP_MARKERS = (
    "that",
    "those",
    "same",
    "the results",
    "the result",
    "sort",
    "chart",
    "export",
    "break down",
    "add ",
)
_MULTI_STEP_SORT_MARKERS = (
    "of the",
    "among the",
    "largest districts",
    "smallest districts",
)
_RANK_MARKERS = ("rank", "highest", "lowest", "top", "bottom", "sort")
_COUNT_FILTER_MARKERS = (
    "how many",
    "count",
    "denominator",
    "percentage",
    "share",
    "how common",
    "more than",
    "less than",
    "at least",
    "not just",
    "regularly",
    "actually",
    "where teachers get watched",
)
_HISTORICAL_MARKERS = (
    "historical",
    "longitudinal",
    "trend",
    "trends",
    "over time",
    "year over year",
    "previous year",
    "last year",
    "since 20",
    "from 20",
)
_PEER_NCES_MARKERS = (
    "peer",
    "peers",
    "comparable",
    "similar districts",
    "demographic",
    "demographically",
    "nces",
    "profile",
)
_RESEARCH_MARKERS = (
    "research",
    "publication",
    "publications",
    "study",
    "studies",
    "evidence",
    "rationale",
    "what does the literature",
)


class PlannerThinkingDeps(Protocol):
    """Minimal dependency surface needed by the thinking selector."""

    message: str
    query_context: object | None
    pending_context: object | None
    # W0.5 (#832): the thinking selector previously checked
    # ``recent_transcript`` to detect followup intent; the proxy is now
    # ``recent_routes`` (non-empty == prior turn happened).
    recent_routes: tuple[str, ...]
    scenario_id: str | None


@dataclass(frozen=True)
class PlannerThinkingDecision:
    """Planner-thinking selection for one model call."""

    policy: PlannerThinkingPolicy
    enabled: bool
    effort: PlannerThinkingEffort | None
    reason: str

    def model_settings_overlay(self) -> dict[str, PlannerThinkingEffort | float]:
        """Return the per-run Pydantic AI model settings overlay."""

        if not self.enabled or self.effort is None:
            return {}
        return {"thinking": self.effort, "temperature": 1.0}


def decide_planner_thinking(
    deps: PlannerThinkingDeps,
    *,
    policy: PlannerThinkingPolicy,
    max_effort: PlannerThinkingEffort,
) -> PlannerThinkingDecision:
    """Return the deterministic planner-thinking decision for a turn."""

    if policy == "off":
        return PlannerThinkingDecision(
            policy=policy,
            enabled=False,
            effort=None,
            reason="policy_off",
        )
    if policy == "all":
        return PlannerThinkingDecision(
            policy=policy,
            enabled=True,
            effort=max_effort,
            reason="policy_all",
        )

    reason = _selected_reason(deps)
    return PlannerThinkingDecision(
        policy=policy,
        enabled=reason is not None,
        effort=max_effort if reason is not None else None,
        reason=reason or "simple_or_unsupported_shape",
    )


def _selected_reason(deps: PlannerThinkingDeps) -> str | None:
    scenario_id = (deps.scenario_id or "").strip()
    if scenario_id in _TARGET_SCENARIO_IDS:
        return "target_scenario"

    text = _normalize_text(deps.message)
    if deps.query_context is not None and _contains_any(text, _FOLLOWUP_MARKERS):
        return "followup_reference"
    if deps.pending_context is not None:
        return "pending_context"
    if deps.recent_routes and _contains_any(text, _FOLLOWUP_MARKERS):
        return "followup_reference"
    if _is_multi_step_selection_sort(text):
        return "multi_step_selection_sort"
    if _contains_any(text, _COUNT_FILTER_MARKERS):
        return "count_filter_denominator"
    if _contains_any(text, _HISTORICAL_MARKERS):
        return "historical_longitudinal"
    if _contains_any(text, _PEER_NCES_MARKERS):
        return "peer_nces_profile"
    if _contains_any(text, _RESEARCH_MARKERS):
        return "research_publication_bundle"
    return None


def _is_multi_step_selection_sort(text: str) -> bool:
    if not _contains_any(text, _MULTI_STEP_SORT_MARKERS):
        return False
    return _contains_any(text, _RANK_MARKERS) or bool(
        re.search(r"\b\d+\s+(largest|smallest|top|bottom)\b", text)
    )


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _normalize_text(value: str) -> str:
    return normalize_whitespace_casefold(value)


def is_unsupported_thinking_error(exc: Exception) -> bool:
    """Return whether a provider explicitly rejected thinking/reasoning settings."""

    text = str(exc).casefold()
    if "compiled grammar is too large" in text:
        return True
    if "strict tools" in text:
        return True
    if "thinking" not in text and "reasoning" not in text:
        return False
    return any(
        marker in text
        for marker in (
            "unsupported",
            "not supported",
            "unrecognized",
            "unknown",
            "invalid",
        )
    )
