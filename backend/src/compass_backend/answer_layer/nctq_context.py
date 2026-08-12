"""Topic resolution and snippet formatting for the answer-layer NCTQ context.

This module is pure (no DB I/O). The async resolver in
``resolve_nctq_context`` takes an injected repository so the orchestration
seam stays testable without a live Postgres.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from compass_backend.contracts.planning import QueryPlan
from compass_backend.contracts.rendering import ResponseManifest


_PROSE_ROOM_ROW_COUNT = 3


SnippetSourceKind = Literal["rationale", "exemplar", "publication"]


class NctqSnippet(BaseModel):
    """One sealed NCTQ context entry for the answer brief."""

    model_config = ConfigDict(extra="forbid")

    source_kind: SnippetSourceKind
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    summary_line: str = Field(min_length=1)
    key_point: str | None = None

    def format(self) -> str:
        """Render the snippet as the opaque string the stylist receives."""

        lines = [
            f"[{self.source_kind}] {self.title} — {self.summary_line}",
            f"URL: {self.url}",
        ]
        if self.key_point:
            lines.append(f"Key point: {self.key_point}")
        return "\n".join(lines)


def has_prose_room(
    manifest: ResponseManifest,
    displayed_row_count: int | None,
) -> bool:
    """Return True when the answer shape has room for a sparing NCTQ aside.

    Triggers on any of:
      - policy_guidance result type
      - rendered manifest carries a non-empty caveat-fragment list
      - displayed_row_count is small but non-zero (1 <= count <= 3)

    A row count of 0 means the answer found no rows to display; injecting
    NCTQ context into an empty result feels like the system dodging the
    miss, so the small-row-count gate explicitly excludes zero.
    """

    if manifest.result_type == "policy_guidance":
        return True
    metadata = manifest.metadata or {}
    if metadata.get("answer_layer_caveat_count"):
        return True
    if (
        displayed_row_count is not None
        and 0 < displayed_row_count <= _PROSE_ROOM_ROW_COUNT
    ):
        return True
    return False


class NctqPublicationHit(BaseModel):
    """One NCTQ publication row fetched for the ``publication`` route.

    Carries the row's title / url / summary VERBATIM (no truncation, no
    rewriting) so the publication renderer can cite NCTQ's published writing
    exactly as stored. The grounding guard checks every cited title/URL in the
    answer body against this fetched set — so these fields are the single source
    of truth for what the answer may claim NCTQ published.
    """

    model_config = ConfigDict(extra="forbid")

    publication_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    summary: str = ""


class NctqContextRepoProtocol(Protocol):
    async def fetch_snippets(
        self,
        topic_keys: tuple[str, ...],
        *,
        limit: int = 2,
    ) -> tuple[NctqSnippet, ...]: ...

    async def search_publications(
        self,
        query: str,
        *,
        limit: int = 3,
    ) -> tuple[NctqPublicationHit, ...]: ...


# Static MVP mapping from metric-name keywords to NCTQ topic keys. The
# planner does not yet expose a first-class metric→topic mapping; this
# table is a deliberate stopgap that covers the 8 curated topics. If a
# new topic lands in compass.nctq_rationales, add it here.
#
# Order: more specific keywords first; dedupe is per-topic, not per-needle,
# so multiple needles can point to the same topic safely.
_METRIC_NAME_TO_TOPIC: tuple[tuple[str, str], ...] = (
    ("starting salary", "general-salary"),
    ("base salary", "general-salary"),
    ("salary schedule", "general-salary"),
    ("salary", "general-salary"),
    ("differentiated", "differentiated-pay"),
    ("hard-to-staff", "differentiated-pay"),
    ("performance pay", "performance-pay"),
    ("bonus", "performance-pay"),
    ("evaluation", "evaluation"),
    ("observation", "evaluation"),
    ("leave", "leave"),
    ("parental", "leave"),
    ("benefits", "benefits"),
    ("health", "benefits"),
    ("retirement", "benefits"),
    ("collective bargaining", "collective-bargaining"),
    ("contract", "collective-bargaining"),
    ("teacher time", "teacher-time"),
    ("planning time", "teacher-time"),
)


def topic_keys_for_plan(plan: QueryPlan) -> tuple[str, ...]:
    """Return canonical NCTQ topic keys implied by the plan's metric names.

    MVP uses a static keyword table because the catalog does not expose a
    metric→topic association yet. Tracked as a follow-up in the spec under
    "Open Questions."
    """

    found: list[str] = []
    seen: set[str] = set()
    for metric in plan.metrics:
        name_lower = metric.name.casefold()
        for needle, topic in _METRIC_NAME_TO_TOPIC:
            if needle in name_lower and topic not in seen:
                found.append(topic)
                seen.add(topic)
    return tuple(found)


def topic_keys_for_policy_guidance(topic_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Return canonical NCTQ topic keys for a policy_guidance turn.

    The planner already resolves typed topic IDs (e.g. ``general-salary``,
    ``benefits``) on the policy_guidance route; this helper just normalizes
    them into a deduped tuple matching the contract of
    ``topic_keys_for_plan``.
    """

    seen: set[str] = set()
    found: list[str] = []
    for topic_id in topic_ids:
        if topic_id not in seen:
            seen.add(topic_id)
            found.append(topic_id)
    return tuple(found)


async def resolve_nctq_context(
    plan: QueryPlan,
    *,
    manifest: ResponseManifest,
    displayed_row_count: int | None,
    repo: NctqContextRepoProtocol,
    limit: int = 2,
) -> tuple[str, ...]:
    """Return formatted NCTQ snippets for the brief, or () when gates fail.

    Both gates must pass:
      1. ``topic_keys_for_plan`` resolves at least one topic.
      2. ``has_prose_room`` is True.
    """

    topic_keys = topic_keys_for_plan(plan)
    if not topic_keys:
        return ()
    if not has_prose_room(manifest, displayed_row_count):
        return ()
    snippets = await repo.fetch_snippets(topic_keys, limit=limit)
    return tuple(snippet.format() for snippet in snippets)


async def resolve_nctq_context_for_policy_guidance(
    topic_ids: tuple[str, ...],
    *,
    manifest: ResponseManifest,
    displayed_row_count: int | None,
    repo: NctqContextRepoProtocol,
    limit: int = 2,
) -> tuple[str, ...]:
    """Resolve NCTQ snippets for a policy_guidance turn."""

    topic_keys = topic_keys_for_policy_guidance(topic_ids)
    if not topic_keys:
        return ()
    if not has_prose_room(manifest, displayed_row_count):
        return ()
    snippets = await repo.fetch_snippets(topic_keys, limit=limit)
    return tuple(snippet.format() for snippet in snippets)


__all__ = [
    "NctqContextRepoProtocol",
    "NctqPublicationHit",
    "NctqSnippet",
    "has_prose_room",
    "resolve_nctq_context",
    "resolve_nctq_context_for_policy_guidance",
    "topic_keys_for_plan",
    "topic_keys_for_policy_guidance",
]
