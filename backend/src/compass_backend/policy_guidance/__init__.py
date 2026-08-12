"""Typed NCTQ policy-guidance content surface for compass_backend.

The runtime-loaded library exposes stances, research rationales, and
exemplary-district policies as immutable Pydantic models. Topic resolution
and rendering layers consume the library deterministically: the planner
picks topic_ids and layers (constrained by Literal[*]) but never authors
the prose itself.

Source of truth (stopgap): ``content/nctq-policy/topics/*.md``. End state
per SSN-236 is a synced cache of NCTQ Pathfinder website content.
"""

from compass_backend.policy_guidance.models import (
    PATHFINDER_PLACEHOLDER,
    CitationStatus,
    ExemplarPolicy,
    GuidanceLayer,
    PolicyGuidanceBundle,
    PolicyGuidanceLibrary,
    ResearchRationale,
    Stance,
    StanceKind,
    TopicGuidance,
)

__all__ = [
    "PATHFINDER_PLACEHOLDER",
    "CitationStatus",
    "ExemplarPolicy",
    "GuidanceLayer",
    "PolicyGuidanceBundle",
    "PolicyGuidanceLibrary",
    "ResearchRationale",
    "Stance",
    "StanceKind",
    "TopicGuidance",
]
