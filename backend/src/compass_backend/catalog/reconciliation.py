"""Unified catalog reconciler.

Fans out one planner-mentioned phrase across every governed catalog
drawer that could legitimately own it (metric, metric_bundle,
profile_rank_field, profile_field, unsupported_concept) and returns a
single :class:`CatalogResolution` ranked by score and ordered by the
caller-supplied :data:`PhraseRoleHint`.

This is step 1 of the catalog-resolution-unification plan
(``docs/plans/2026-05-28-catalog-resolution-unification-plan.md``). The
reconciler ships in shadow: no pipeline calls it yet. Step 5 of the
plan flips the planner output validator to consume reconciler output
instead of running per-resolver classifications.

The reconciler's job is to make the FRPL/enrollment/BA-salary family of
failures impossible at the recognition layer: when the catalog has an
approved alias for a phrase, that alias must drive classification, not
the slot of the planner draft the phrase happens to live in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from compass_backend.catalog.profile_rank_fields import (
    ProfileRankField,
    profile_rank_field_by_key,
)
from compass_backend.catalog.resolution import (
    CatalogResolutionEntity,
    MetricBundleResolution,
    MetricCandidate,
    ProfileFieldResolution,
)
from compass_backend.contracts.catalog_resolution import (
    CatalogReconciliationReport,
    CatalogResolution,
    PhraseRoleHint,
    ResolutionConfidence,
    ResolvedEntity,
    ResolvedEntityType,
)


@dataclass(frozen=True)
class PhraseToReconcile:
    """One phrase the reconciler should resolve, plus its role context."""

    phrase: str
    role_hint: PhraseRoleHint = "unspecified"


class _ReconcilerCatalog(Protocol):
    """Minimal catalog surface the reconciler needs.

    Matches the relevant subset of
    :class:`compass_backend.catalog.resolution.CatalogResolver` so tests
    can supply a fake without implementing the full resolver surface.
    """

    async def resolve_metric_bundle(
        self,
        query: str,
        *,
        numeric_only: bool = False,
        limit: int = 5,
        degree_lane: object | None = None,
    ) -> MetricBundleResolution: ...

    async def resolve_profile_field_authority(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> ProfileFieldResolution: ...

    async def resolve_unsupported_concepts(
        self,
        phrases: list[str],
    ) -> list[CatalogResolutionEntity]: ...


# Scoring scale. All scorers map to [0.0, 1.0]; the constants below
# anchor the meaningful tiers so confidence rules can reason about
# spread without re-tuning per catalog drawer.
_SCORE_EXACT_ALIAS = 1.0
_SCORE_EXACT_KEY = 0.95
_SCORE_RESOLVED_SINGLE = 0.9
_SCORE_CANDIDATE = 0.7
_SCORE_AMBIGUOUS_CANDIDATE = 0.6

# Role-hint preferences. Higher rank = the catalog drawer wins ties.
# The hint is non-authoritative — it never invents a drawer the catalog
# did not return, only orders ties among drawers that did.
_ROLE_PREFERENCE: dict[PhraseRoleHint, tuple[ResolvedEntityType, ...]] = {
    "sort_key": (
        "profile_rank_field",
        "metric",
        "metric_bundle",
        "profile_field",
    ),
    "selection_filter": (
        "profile_rank_field",
        "metric",
        "metric_bundle",
        "profile_field",
    ),
    "metric_subject": (
        "metric",
        "metric_bundle",
        "profile_field",
        "profile_rank_field",
    ),
    "profile_subject": (
        "profile_field",
        "profile_rank_field",
        "metric",
        "metric_bundle",
    ),
    "selection_district": ("district",),
    "peer_anchor": ("peer_set", "district"),
    "topic_subject": ("topic", "metric_bundle", "metric"),
    "unspecified": (),
}


class CatalogReconciler:
    """Single resolver entry point for one planner-mentioned phrase.

    Construct once per request and call :meth:`reconcile` (or
    :meth:`reconcile_all` for a batch) — the reconciler is stateless
    beyond a memoized snapshot of the
    :class:`~compass_backend.catalog.profile_rank_fields.ProfileRankField`
    enum, which is fixed for the process lifetime.
    """

    def __init__(self, catalog: _ReconcilerCatalog) -> None:
        self._catalog = catalog
        self._profile_rank_keys: dict[str, ProfileRankField] = (
            profile_rank_field_by_key()
        )

    async def reconcile(
        self,
        phrase: str,
        *,
        role_hint: PhraseRoleHint = "unspecified",
    ) -> CatalogResolution:
        """Resolve one phrase across every governed catalog drawer."""

        cleaned = phrase.strip()
        if not cleaned:
            return CatalogResolution(
                phrase=phrase,
                role_hint=role_hint,
                candidates=(),
                contextual_default=None,
                confidence="none",
            )

        # #1587: a bare asyncio.gather() (default return_exceptions=False)
        # unwinds the parent on the first child failure while the other
        # children keep running detached against the per-turn DB connection
        # (db/_turn_connection.py) -- a use-after-release once the parent leaves
        # the turn_connection scope. We use gather(return_exceptions=True) here
        # (not TaskGroup) because this reconcile path runs inside the planner
        # agent's tool, where orchestration catches the *specific*
        # ``UnexpectedModelBehavior`` (orchestration/chat.py) and Pydantic AI's
        # retry control flow keys off exact exception types; an ExceptionGroup
        # from TaskGroup would change the surfaced type. Collecting all results
        # before reraising still guarantees every sibling settled before the
        # parent unwinds, which closes the detached-sibling hazard while
        # preserving the original exception type.
        bundle, profile, unsupported = await asyncio.gather(
            self._catalog.resolve_metric_bundle(cleaned, numeric_only=False),
            self._catalog.resolve_profile_field_authority(cleaned),
            self._catalog.resolve_unsupported_concepts([cleaned]),
            return_exceptions=True,
        )
        for result in (bundle, profile, unsupported):
            if isinstance(result, BaseException):
                raise result

        candidates: list[ResolvedEntity] = []
        candidates.extend(_entities_from_unsupported(unsupported))
        candidates.extend(_entities_from_bundle(bundle))
        candidates.extend(_entities_from_profile(profile))
        candidates.extend(self._entities_from_profile_rank(cleaned, profile))

        ordered = _rank_candidates(candidates, role_hint=role_hint)
        confidence = _classify_confidence(ordered)
        return CatalogResolution(
            phrase=phrase,
            role_hint=role_hint,
            candidates=tuple(ordered),
            contextual_default=None,
            confidence=confidence,
        )

    async def reconcile_all(
        self,
        phrases: Iterable[PhraseToReconcile],
    ) -> CatalogReconciliationReport:
        """Resolve a batch of phrases in parallel.

        Duplicate phrases (case-insensitive, stripped) collapse to one
        reconciliation. When the same phrase appears in multiple slots
        with different role hints, the highest-priority hint wins per
        :data:`_ROLE_HINT_PRIORITY` — this is the fix for the
        SORT-NEW-02 shape where the planner emits ``enrollment`` in
        both ``metrics`` and ``sort.field`` and the finalizer would
        otherwise see the metric-context resolution where the
        sort-context one is what determines executor dispatch.
        """

        chosen: dict[str, PhraseToReconcile] = {}
        for entry in phrases:
            key = entry.phrase.casefold().strip()
            if not key:
                continue
            existing = chosen.get(key)
            if existing is None or _role_hint_priority(
                entry.role_hint
            ) > _role_hint_priority(existing.role_hint):
                chosen[key] = entry

        # #1587: gather(return_exceptions=True) + reraise, not TaskGroup, for
        # the same reason as reconcile() above. reconcile_all runs inside the
        # planner agent's output validator (planning/planner.py →
        # reconcile_and_finalize), where Pydantic AI's retry control flow and
        # orchestration's ``except UnexpectedModelBehavior`` (orchestration/
        # chat.py) key off exact exception types -- an ExceptionGroup from a
        # TaskGroup would change the surfaced type and bypass them. A bare
        # gather() (default return_exceptions=False) would unwind the parent on
        # the first child failure while siblings keep running detached against
        # the per-turn DB connection (db/_turn_connection.py); collecting every
        # result before reraising the first exception guarantees all siblings
        # settled before the parent unwinds, closing that hazard, while the
        # reraise preserves the original (non-grouped) exception type.
        resolutions = await asyncio.gather(
            *(
                self.reconcile(entry.phrase, role_hint=entry.role_hint)
                for entry in chosen.values()
            ),
            return_exceptions=True,
        )
        for resolution in resolutions:
            if isinstance(resolution, BaseException):
                raise resolution
        return CatalogReconciliationReport(resolutions=tuple(resolutions))

    def _entities_from_profile_rank(
        self,
        phrase: str,
        profile: ProfileFieldResolution,
    ) -> list[ResolvedEntity]:
        """Emit profile_rank_field candidates from two paths.

        The rank-fields enum is the rankable subset of NCES profile
        fields — currently ``enrollment`` and ``frpl_pct``. A phrase
        produces a ``profile_rank_field`` candidate when either:

        1. The phrase equals a rank-field key or label (case-insensitive,
           stripped). Covers the SORT-NEW-02 / case 8 "rank by enrollment"
           path where the user names the field directly.
        2. The profile-field resolver landed on a key that happens to be
           in the rank-fields enum. Covers the row 20 / case 13 "free and
           reduced lunch share" path where the catalog's alias row
           ``frpl_pct`` is approved for ranking via the
           :class:`ProfileRankField` enum.

        Both paths emit the same kind of candidate; the role hint later
        picks which drawer (profile_rank_field vs profile_field) the
        finalizer should dispatch through.
        """

        norm = phrase.casefold().strip()
        hits: list[ResolvedEntity] = []
        seen_keys: set[str] = set()

        for field in self._profile_rank_keys.values():
            if norm in {field.field_key.casefold(), field.label.casefold()}:
                hits.append(self._rank_field_entity(field, score=_SCORE_EXACT_KEY))
                seen_keys.add(field.field_key)

        resolved = profile.resolved
        if resolved is not None and resolved.field_key in self._profile_rank_keys:
            if resolved.field_key not in seen_keys:
                field = self._profile_rank_keys[resolved.field_key]
                score = (
                    _SCORE_EXACT_ALIAS
                    if profile.resolution_method == "alias"
                    else _SCORE_EXACT_KEY
                )
                hits.append(self._rank_field_entity(field, score=score))
                seen_keys.add(field.field_key)

        return hits

    @staticmethod
    def _rank_field_entity(
        field: ProfileRankField, *, score: float
    ) -> ResolvedEntity:
        return ResolvedEntity(
            entity_type="profile_rank_field",
            catalog_id=f"profile_rank_field:{field.field_key}",
            label=field.label,
            score=score,
            vintage_label=field.vintage_label,
            source="profile_rank_field_by_key",
            metadata={
                "field_key": field.field_key,
                "column_name": field.column_name,
                "display": field.display,
            },
        )


def _entities_from_unsupported(
    entities: list[CatalogResolutionEntity],
) -> list[ResolvedEntity]:
    """Promote unsupported-concept refusals to top-priority candidates.

    An approved out-of-universe alias is a hard block — the resolver
    always returns it as the primary, so downstream finalizers convert
    to refusal without consulting other candidates.
    """

    resolved: list[ResolvedEntity] = []
    for entity in entities:
        if entity.entity_type != "unsupported_concept":
            continue
        if not entity.approved_key:
            continue
        resolved.append(
            ResolvedEntity(
                entity_type="unsupported_concept",
                catalog_id=f"unsupported_concept:{entity.approved_key}",
                label=entity.label or entity.input_phrase,
                score=_SCORE_EXACT_ALIAS,
                vintage_label=None,
                source="resolve_unsupported_concepts",
                metadata={
                    "input_phrase": entity.input_phrase,
                    "message": entity.message or "",
                    "provenance": entity.provenance,
                },
            )
        )
    return resolved


def _entities_from_bundle(
    bundle: MetricBundleResolution,
) -> list[ResolvedEntity]:
    """Project a :class:`MetricBundleResolution` into reconciler candidates."""

    entities: list[ResolvedEntity] = []
    resolved_ids: set[int] = set()

    if bundle.bundle_key and bundle.resolved:
        entities.append(
            ResolvedEntity(
                entity_type="metric_bundle",
                catalog_id=f"metric_bundle:{bundle.bundle_key}",
                label=bundle.bundle_key,
                score=_SCORE_EXACT_ALIAS if not bundle.ambiguous else _SCORE_AMBIGUOUS_CANDIDATE,
                vintage_label=None,
                source="resolve_metric_bundle",
                metadata={
                    "metric_ids": [m.metric_id for m in bundle.resolved],
                    "bundle_key": bundle.bundle_key,
                    "ambiguous": bundle.ambiguous,
                },
            )
        )

    if not bundle.bundle_key and len(bundle.resolved) == 1 and not bundle.ambiguous:
        metric = bundle.resolved[0]
        resolved_ids.add(metric.metric_id)
        entities.append(_metric_entity(metric, score=_SCORE_RESOLVED_SINGLE))

    candidate_score = (
        _SCORE_AMBIGUOUS_CANDIDATE if bundle.ambiguous else _SCORE_CANDIDATE
    )
    for candidate in bundle.candidates:
        if candidate.metric_id in resolved_ids:
            continue
        entities.append(_metric_entity(candidate, score=candidate_score))

    return entities


def _metric_entity(metric: MetricCandidate, *, score: float) -> ResolvedEntity:
    return ResolvedEntity(
        entity_type="metric",
        catalog_id=f"metric:{metric.metric_id}",
        label=metric.name,
        score=score,
        vintage_label=None,
        source="resolve_metric_bundle",
        metadata={
            "metric_id": metric.metric_id,
            "answer_type": metric.answer_type,
            "topic": metric.topic,
        },
    )


def _entities_from_profile(
    profile: ProfileFieldResolution,
) -> list[ResolvedEntity]:
    """Project a :class:`ProfileFieldResolution` into reconciler candidates."""

    entities: list[ResolvedEntity] = []
    resolved_keys: set[str] = set()

    if profile.resolved is not None:
        resolved = profile.resolved
        resolved_keys.add(resolved.field_key)
        score = (
            _SCORE_EXACT_ALIAS
            if profile.resolution_method == "alias"
            else _SCORE_EXACT_KEY
        )
        entities.append(
            ResolvedEntity(
                entity_type="profile_field",
                catalog_id=f"profile_field:{resolved.field_key}",
                label=resolved.label,
                score=score,
                vintage_label=None,
                source=f"resolve_profile_field_authority/{profile.resolution_method}",
                metadata={
                    "field_key": resolved.field_key,
                    "data_type": resolved.data_type,
                },
            )
        )

    for candidate in profile.candidates:
        if candidate.field_key in resolved_keys:
            continue
        entities.append(
            ResolvedEntity(
                entity_type="profile_field",
                catalog_id=f"profile_field:{candidate.field_key}",
                label=candidate.label,
                score=_SCORE_CANDIDATE,
                vintage_label=None,
                source="resolve_profile_field_authority",
                metadata={
                    "field_key": candidate.field_key,
                    "data_type": candidate.data_type,
                },
            )
        )
    return entities


# Per-rank role-hint penalty. Small enough that a clearly better catalog
# match (e.g. an exact alias) still wins regardless of role, but large
# enough to break ties between equally-scored candidates from different
# drawers. The penalty is applied to the *effective* sort score only;
# the raw :attr:`ResolvedEntity.score` returned to consumers is
# unchanged so debugging stays honest.
_ROLE_PREFERENCE_PENALTY = 0.05


# When the same phrase appears in multiple slots with different role
# hints (e.g. the planner puts "enrollment" in both ``metrics`` and
# ``sort.field``), :meth:`CatalogReconciler.reconcile_all` keeps only
# one reconciliation per phrase to avoid duplicate catalog calls. The
# kept role hint is the highest-priority one per this table — the
# downstream finalizer dispatches off resolution.primary, and sort
# context binds the executor to a specific drawer (rank_districts_by_
# profile_field vs metric lookup) where the metric context only binds
# to drawer-rebalancing. Higher priority = wins.
_ROLE_HINT_PRIORITY: dict[str, int] = {
    "sort_key": 100,
    "selection_filter": 80,
    "selection_district": 70,
    "peer_anchor": 60,
    "profile_subject": 50,
    "metric_subject": 40,
    "topic_subject": 30,
    "unspecified": 0,
}


def _role_hint_priority(role_hint: PhraseRoleHint) -> int:
    return _ROLE_HINT_PRIORITY.get(role_hint, 0)


def _preference_rank(
    entity: ResolvedEntity,
    preference: tuple[ResolvedEntityType, ...],
) -> int:
    try:
        return preference.index(entity.entity_type)
    except ValueError:
        return len(preference)


def _rank_candidates(
    candidates: list[ResolvedEntity],
    *,
    role_hint: PhraseRoleHint,
) -> list[ResolvedEntity]:
    """Order candidates by effective score, with role-hint as a penalty.

    ``unsupported_concept`` always sorts first when present — it is a
    refusal and downstream consumers must see it before any
    classification candidate.
    """

    if not candidates:
        return []

    refusals = [c for c in candidates if c.entity_type == "unsupported_concept"]
    rest = [c for c in candidates if c.entity_type != "unsupported_concept"]

    preference = _ROLE_PREFERENCE.get(role_hint, ())

    def sort_key(entity: ResolvedEntity) -> tuple[float, str]:
        penalty = _ROLE_PREFERENCE_PENALTY * _preference_rank(entity, preference)
        return (-(entity.score - penalty), entity.catalog_id)

    rest.sort(key=sort_key)
    return refusals + rest


def _same_underlying_entity(a: ResolvedEntity, b: ResolvedEntity) -> bool:
    """Return True when two candidates point at the same catalog row.

    ``profile_rank_field:enrollment`` and ``profile_field:enrollment``
    are different drawers for the same NCES field; the role hint
    cleanly picks which drawer the executor should dispatch through.
    The reconciler treats this as high confidence — there is no real
    classification ambiguity for the adjudicator to break.
    """

    a_field = a.metadata.get("field_key")
    b_field = b.metadata.get("field_key")
    if a_field and b_field:
        return a_field == b_field
    a_metric = a.metadata.get("metric_id")
    b_metric = b.metadata.get("metric_id")
    if a_metric and b_metric:
        return a_metric == b_metric
    return False


def _classify_confidence(
    ordered: list[ResolvedEntity],
) -> ResolutionConfidence:
    """Bucket the ordered candidate list into a confidence tier.

    See :data:`ResolutionConfidence` for tier semantics.
    """

    if not ordered:
        return "none"

    top = ordered[0]
    if top.entity_type == "unsupported_concept":
        return "high"

    if len(ordered) == 1:
        if top.score >= _SCORE_RESOLVED_SINGLE:
            return "high"
        if top.score >= _SCORE_CANDIDATE:
            return "medium"
        return "ambiguous"

    second = ordered[1]
    if _same_underlying_entity(top, second):
        if top.score >= _SCORE_CANDIDATE:
            return "high"
        return "medium"

    spread = top.score - second.score
    if top.score >= _SCORE_RESOLVED_SINGLE and spread >= 0.2:
        return "high"
    if spread >= 0.15:
        return "medium"
    return "ambiguous"
