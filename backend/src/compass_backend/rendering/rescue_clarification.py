"""Typed registry mapping `RescueClarificationCode` → user-facing prose.

The registry is exhaustive over the `RescueClarificationCode` Literal. A
missing entry surfaces as a `KeyError` at resolve time and as a failure in
`test_rescue_clarification_registry_is_exhaustive` — both signals that a new
code was added to the Literal without a registry entry.

Mirrors the `MethodologyCode` registry pattern in `methodology.py`. The chat
orchestrator constructs a typed `RescueClarificationRef` from typed
`QueryContext` fields (district count, metric name) and resolves it here.
Orchestration code MUST NOT inline-f-string user-facing prose — keeping the
prose templates in the rendering layer is the structural form of the
CLAUDE.md "no prose-dispatch below the planner" doctrine. Inputs are typed,
outputs are typed-template renders.
"""

from __future__ import annotations

from typing import Callable, Literal, get_args

from pydantic import BaseModel, Field

RescueClarificationCode = Literal[
    "anchored_with_metric",
    "anchored_no_metric",
    "fallback_no_anchor",
]


class RescueClarificationRef(BaseModel):
    """Typed reference to a rescue clarification template.

    Carries typed inputs only; the registry maps each code to a function
    that renders the user-facing question from these fields.
    """

    model_config = {"frozen": True}

    code: RescueClarificationCode
    district_count: int = Field(default=0, ge=0)
    metric_name: str | None = None


def _anchored_with_metric_line(ref: RescueClarificationRef) -> str:
    count = ref.district_count
    name = ref.metric_name or ""
    return (
        f"I have your prior turn's result on file — "
        f"{count} district(s) about \"{name}\". "
        "I couldn't structure this follow-up against that "
        "context. Could you rephrase what you'd like? For "
        "example: "
        f"\"show those {count} districts and a specific "
        "metric\", \"sort that list by …\", or \"compare those "
        "against a different metric\"."
    )


def _anchored_no_metric_line(ref: RescueClarificationRef) -> str:
    count = ref.district_count
    return (
        f"I have your prior turn's result on file — "
        f"{count} district(s). I couldn't structure "
        "this follow-up against that context. Could you "
        "rephrase what you'd like? For example: "
        f"\"show those {count} districts with "
        "[metric name]\", or \"sort that list by …\"."
    )


def _fallback_no_anchor_line(ref: RescueClarificationRef) -> str:
    return (
        "I need one district group and one Compass metric to start. "
        "Could you rephrase? For example: "
        "\"show the top 10 districts ranked by [one metric]\", "
        "\"compare [two or three districts] on [one or more metrics]\", "
        "\"show separate rankings for [metric A] and [metric B]\", or "
        "\"list districts where [metric] is greater than "
        "[value]\"."
    )


_LineFn = Callable[[RescueClarificationRef], str]

_RESCUE_CLARIFICATION_REGISTRY: dict[RescueClarificationCode, _LineFn] = {
    "anchored_with_metric": _anchored_with_metric_line,
    "anchored_no_metric": _anchored_no_metric_line,
    "fallback_no_anchor": _fallback_no_anchor_line,
}


def rescue_clarification_question_for(ref: RescueClarificationRef) -> str:
    """Resolve a `RescueClarificationRef` into its user-facing question."""

    return _RESCUE_CLARIFICATION_REGISTRY[ref.code](ref)


def registered_rescue_clarification_codes() -> frozenset[RescueClarificationCode]:
    """Set of rescue clarification codes the registry currently maps."""

    return frozenset(_RESCUE_CLARIFICATION_REGISTRY.keys())


def all_rescue_clarification_codes() -> frozenset[RescueClarificationCode]:
    """Set of every rescue clarification code declared on the Literal."""

    return frozenset(get_args(RescueClarificationCode))
