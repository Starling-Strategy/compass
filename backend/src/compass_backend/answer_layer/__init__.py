"""Guarded answer-layer helpers for Compass."""

from compass_backend.answer_layer.nctq_context import (
    NctqContextRepoProtocol,
    NctqSnippet,
    resolve_nctq_context,
    resolve_nctq_context_for_policy_guidance,
    topic_keys_for_plan,
    topic_keys_for_policy_guidance,
)

__all__ = [
    "NctqContextRepoProtocol",
    "NctqSnippet",
    "resolve_nctq_context",
    "resolve_nctq_context_for_policy_guidance",
    "topic_keys_for_plan",
    "topic_keys_for_policy_guidance",
]
