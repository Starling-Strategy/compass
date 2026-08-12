"""Planning layer for the fresh Compass pipeline."""

# ``thinking`` is imported eagerly so that ``compass_backend.agents.hooks``
# (which imports ``is_unsupported_thinking_error`` from this subpackage at
# module load) can fully initialize without triggering the rest of this
# package's heavy imports. The rest of the public surface is exposed via
# PEP 562 ``__getattr__`` so submodule names import lazily — necessary
# because ``planner.py`` (and friends) now depend on
# ``compass_backend.agents.hooks``, which would otherwise force a circular
# import through this ``__init__``.
from typing import TYPE_CHECKING

from .thinking import PlannerThinkingDecision, decide_planner_thinking

if TYPE_CHECKING:
    # Declare the lazy ``__getattr__`` surface statically so type checkers,
    # IDEs, and CodeQL's ``import *`` analysis (py/undefined-export) see these
    # names as defined. Guarded by ``TYPE_CHECKING`` so it never runs at
    # import time — preserving the PEP 562 lazy loading that exists to break
    # the ``planner`` -> ``agents.hooks`` circular import documented above.
    from .planner import (
        CachedPlannerAgent,
        PlannerAgent,
        PlannerDeps,
        PlannerRun,
        create_planner_agent,
        merge_turn_with_session_context,
        normalize_ba_ma_salary_rank_turn,
        pending_context_after_turn,
        pending_context_from_execution_clarification,
        planner_context_instructions,
        planner_guidance_authority_warning,
        planner_guidance_instructions,
        planner_runtime_context_instructions,
        promote_pending_context_to_plan,
        run_planner,
        run_planner_turn,
        validate_planner_turn_quality,
    )
    from .profile_fields import (
        normalize_plan_profile_ranking_intent,
        normalize_turn_profile_ranking_intent,
    )
    from .temporal import (
        academic_year_window_ending_at,
        normalize_academic_year,
        normalize_plan_temporal_intent,
        normalize_turn_temporal_intent,
    )

_LAZY_EXPORTS: dict[str, str] = {
    "CachedPlannerAgent": ".planner",
    "PlannerAgent": ".planner",
    "PlannerDeps": ".planner",
    "PlannerRun": ".planner",
    "create_planner_agent": ".planner",
    "merge_turn_with_session_context": ".planner",
    "normalize_ba_ma_salary_rank_turn": ".planner",
    "pending_context_after_turn": ".planner",
    "pending_context_from_execution_clarification": ".planner",
    "planner_context_instructions": ".planner",
    "planner_guidance_authority_warning": ".planner",
    "planner_guidance_instructions": ".planner",
    "planner_runtime_context_instructions": ".planner",
    "promote_pending_context_to_plan": ".planner",
    "run_planner": ".planner",
    "run_planner_turn": ".planner",
    "validate_planner_turn_quality": ".planner",
    "normalize_plan_profile_ranking_intent": ".profile_fields",
    "normalize_turn_profile_ranking_intent": ".profile_fields",
    "academic_year_window_ending_at": ".temporal",
    "normalize_academic_year": ".temporal",
    "normalize_plan_temporal_intent": ".temporal",
    "normalize_turn_temporal_intent": ".temporal",
}


def __getattr__(name: str) -> object:
    submodule_path = _LAZY_EXPORTS.get(name)
    if submodule_path is None:
        raise AttributeError(f"module 'compass_backend.planning' has no attribute {name!r}")
    from importlib import import_module

    module = import_module(submodule_path, __name__)
    value = getattr(module, name)
    globals()[name] = value  # cache for subsequent attribute access
    return value

__all__ = [
    "CachedPlannerAgent",
    "PlannerAgent",
    "PlannerDeps",
    "PlannerRun",
    "create_planner_agent",
    "merge_turn_with_session_context",
    "normalize_academic_year",
    "normalize_plan_profile_ranking_intent",
    "normalize_plan_temporal_intent",
    "normalize_ba_ma_salary_rank_turn",
    "normalize_turn_profile_ranking_intent",
    "normalize_turn_temporal_intent",
    "pending_context_after_turn",
    "pending_context_from_execution_clarification",
    "planner_context_instructions",
    "planner_guidance_authority_warning",
    "planner_guidance_instructions",
    "planner_runtime_context_instructions",
    "promote_pending_context_to_plan",
    "PlannerThinkingDecision",
    "decide_planner_thinking",
    "run_planner",
    "run_planner_turn",
    "validate_planner_turn_quality",
    "academic_year_window_ending_at",
]
