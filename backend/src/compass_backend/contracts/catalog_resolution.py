"""Unified catalog-resolution contracts.

The recognition advisor today routes each planner-mentioned phrase to a
single typed resolver (metric, profile field, district, etc.) based on
which slot of the planner draft the phrase came from. This couples the
classification decision to the planner's box choice, which the planner
makes before the catalog has been consulted. When the planner guesses
wrong (puts ``enrollment`` in ``metrics`` or ``sort.field`` instead of
``profile_fields`` / ``sort_steps[key_type=profile_field]``), the
downstream block reads as "Compass could not find a governed metric for
'enrollment'" even though the governed profile-field row exists.

This module's contracts are the unified-resolver target. One
:class:`CatalogResolution` per planner-mentioned phrase, listing every
catalog drawer that could legitimately own the phrase, with a confidence
that downstream consumers (adjudicator, plan finalizer, validators) use
to decide whether to route, clarify, or refuse.

Step 1 of the catalog-resolution-unification plan adds the contracts and
the :class:`CatalogReconciler` that produces them. No pipeline wiring;
the existing recognition advisor still runs unchanged.

See ``docs/plans/2026-05-28-catalog-resolution-unification-plan.md``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ResolvedEntityType = Literal[
    "metric",
    "metric_bundle",
    "profile_rank_field",
    "profile_field",
    "district",
    "unsupported_concept",
    "peer_set",
    "topic",
]
"""Catalog drawers a planner phrase can resolve to.

``profile_rank_field`` and ``profile_field`` are kept distinct on
purpose. ``profile_rank_field`` is the rankable subset (currently
``enrollment``, ``frpl_pct`` — see
:mod:`compass_backend.catalog.profile_rank_fields`); ``profile_field``
covers the broader NCES allowlist. Two types let the plan finalizer
reject "rank by ``<non-rankable profile_field>``" with a real candidate
list at finalize time instead of relying on
:class:`UnsupportedRankField` blowing up at executor dispatch.
"""

PhraseRoleHint = Literal[
    "sort_key",
    "selection_filter",
    "selection_district",
    "metric_subject",
    "profile_subject",
    "peer_anchor",
    "topic_subject",
    "unspecified",
]
"""What role the phrase plays in the planner's draft.

Used as a tie-breaker among same-scored candidates from different
catalog drawers. For example, ``enrollment`` resolves through both the
NCES alias path (``profile_field``) and the rank-fields enum
(``profile_rank_field``); with ``role_hint="sort_key"`` the rank-fields
entry wins, with ``role_hint="profile_subject"`` the NCES alias wins.

The hint is non-authoritative: it never invents an entity type that the
catalog didn't return, only orders ties that the catalog did return.
"""

ResolutionConfidence = Literal["high", "medium", "ambiguous", "none"]
"""How confident the reconciler is in its primary-candidate pick.

- ``high``: a single candidate dominates; downstream adjudicator can be
  skipped. Emit ``compass.catalog.adjudicate.skipped`` for audit.
- ``medium``: multiple plausible candidates but one leads by a clear
  margin; adjudicator picks with low-risk LLM input.
- ``ambiguous``: top candidates are close in score across different
  entity types; clarification is the right route.
- ``none``: catalog has no candidates for this phrase — either a true
  out-of-universe concept or a catalog data gap. The downstream caller
  decides which (the new pipeline makes this distinction observable).
"""


class ResolvedEntity(BaseModel):
    """One typed catalog candidate for a planner-mentioned phrase.

    ``catalog_id`` carries the typed reference downstream consumers need
    to actually authorize execution — e.g. ``"metric:123"``,
    ``"profile_rank_field:enrollment"``, ``"district:456"``. It is the
    durable handle for the entity; the ``label`` is for prose only.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_type: ResolvedEntityType
    catalog_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)
    vintage_label: str | None = None
    source: str = Field(
        min_length=1,
        description=(
            "Which resolver method produced this candidate. Kept for "
            "debugging the reconciler's fan-out; never exposed to the "
            "planner prompt."
        ),
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class CatalogResolution(BaseModel):
    """Reconciliation result for one planner-mentioned phrase.

    The reconciler queries every catalog drawer the phrase could
    legitimately live in and returns every plausible candidate, ranked
    by score with the ``role_hint`` applied as a tie-breaker. Downstream
    consumers decide the route: high confidence → finalize directly,
    medium → adjudicate, ambiguous → clarify with candidates, none →
    surface the catalog gap.
    """

    model_config = ConfigDict(extra="forbid")

    phrase: str = Field(min_length=1)
    role_hint: PhraseRoleHint
    candidates: tuple[ResolvedEntity, ...] = Field(default_factory=tuple)
    contextual_default: ResolvedEntity | None = Field(
        default=None,
        description=(
            "Approved default for a bounded context, e.g. the "
            "``frpl_profile_sort_display`` alias. When present this is "
            "the primary; the reconciler still returns the underlying "
            "``candidates`` for audit and adjudicator input."
        ),
    )
    confidence: ResolutionConfidence

    @property
    def primary(self) -> ResolvedEntity | None:
        """Return the entity the finalizer should treat as authoritative.

        ``contextual_default`` wins when present (it's already a
        catalog-approved bounded-context override). Otherwise the
        highest-scored candidate, or ``None`` if no candidates at all.
        """
        if self.confidence == "none":
            return None
        if self.contextual_default is not None:
            return self.contextual_default
        if not self.candidates:
            return None
        return self.candidates[0]

    @property
    def is_ambiguous(self) -> bool:
        """Convenience for the adjudicator dispatch decision."""

        return self.confidence == "ambiguous"


class CatalogReconciliationReport(BaseModel):
    """All reconciliations for one planner draft."""

    model_config = ConfigDict(extra="forbid")

    resolutions: tuple[CatalogResolution, ...] = Field(default_factory=tuple)

    def for_phrase(self, phrase: str) -> CatalogResolution | None:
        """Return the resolution for ``phrase`` (case-insensitive, stripped).

        Returns ``None`` if the reconciler did not process this phrase
        in this report.
        """

        norm = phrase.casefold().strip()
        for resolution in self.resolutions:
            if resolution.phrase.casefold().strip() == norm:
                return resolution
        return None

    @property
    def has_blockers(self) -> bool:
        """True if any phrase resolved to an ``unsupported_concept`` primary.

        ``unsupported_concept`` is a refusal — finalizer turns it into
        an :class:`ExecutionRefusal`-shaped clarify.
        """

        return any(
            resolution.primary is not None
            and resolution.primary.entity_type == "unsupported_concept"
            for resolution in self.resolutions
        )

    @property
    def has_unresolved(self) -> bool:
        """True if any phrase resolved with ``confidence="none"``.

        Distinct from :attr:`has_blockers` — an unresolved phrase is a
        catalog gap (no candidates anywhere), not an out-of-universe
        refusal. The finalizer routes these to clarify; observability
        promotes them to backlog items.
        """

        return any(
            resolution.confidence == "none" for resolution in self.resolutions
        )
