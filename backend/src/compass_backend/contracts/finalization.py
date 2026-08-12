"""Plan-finalization contracts.

Step 4 of the catalog-resolution-unification plan
(``docs/plans/2026-05-28-catalog-resolution-unification-plan.md``). The
finalizer is a pure function that takes a planner draft + a
:class:`~compass_backend.contracts.catalog_resolution.CatalogReconciliationReport`
and returns a :class:`FinalizedPlan` — the slot-corrected
:class:`~compass_backend.contracts.planning.QueryPlan` plus typed
blockers for cases that need clarify/refuse routing.

The design invariant is that the finalizer never invents an entity:
every :class:`SlotMove` references the
:class:`~compass_backend.contracts.catalog_resolution.ResolvedEntity`
that justified the move, and every :class:`FinalizerBlocker` carries
catalog evidence too. This is what keeps the finalizer from drifting
into another opaque classifier the way the recognition advisor's
``_looks_like_frpl`` heuristic did.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from compass_backend.contracts.catalog_resolution import ResolvedEntity
from compass_backend.contracts.planning import QueryPlan

SlotMoveKind = Literal[
    "sort_into_sort_steps",
    "metric_into_profile_fields",
    "profile_field_into_metrics",
    "sort_step_key_type_corrected",
]
"""What kind of slot transformation the finalizer made.

- ``sort_into_sort_steps`` — legacy :class:`SortSpec` collapsed into
  :class:`SortStepSpec`. The decision the plan ultimately retires
  :class:`SortSpec` via step 8.
- ``metric_into_profile_fields`` — a phrase the planner put in
  ``metrics`` resolved to an NCES profile field. Closes the family-A
  shape where the planner saw "enrollment" or "FRPL share" and shoved
  it into the metric slot.
- ``profile_field_into_metrics`` — symmetric: a phrase in
  ``profile_fields`` resolved to a governed policy metric.
- ``sort_step_key_type_corrected`` — the planner emitted the right
  field name but the wrong :data:`SortKeyType`. The finalizer flips
  the key_type without moving the field.
"""


class SlotMove(BaseModel):
    """One slot transformation the finalizer made.

    Every slot move references the :class:`ResolvedEntity` that
    justified it. Step-6 audit of "did the finalizer move slots we did
    not authorize?" is a structured grep over this list.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SlotMoveKind
    phrase: str = Field(min_length=1)
    from_slot: str = Field(
        min_length=1,
        description=(
            "Human-readable path of the original slot, e.g. "
            "``metrics[0]`` or ``sort.field``. Format is for audit "
            "and telemetry — not parsed back into the plan."
        ),
    )
    to_slot: str = Field(min_length=1)
    resolved_entity: ResolvedEntity


class UnsupportedConceptBlocker(BaseModel):
    """Phrase resolved to a governed unsupported-concept alias.

    The catalog has already flagged this phrase as outside the
    universe. The validator turns this into a refusal — not a clarify
    — because the catalog has already done that decision work.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["unsupported_concept"] = "unsupported_concept"
    phrase: str = Field(min_length=1)
    source_field: str | None = None
    catalog_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class UnresolvedPhraseBlocker(BaseModel):
    """Phrase has no candidates anywhere in the catalog.

    Distinct from :class:`UnsupportedConceptBlocker` — an unresolved
    phrase is a catalog data gap (no governed row covers it), not an
    out-of-universe refusal. The validator routes these to clarify;
    observability promotes them to backlog items.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["unresolved_phrase"] = "unresolved_phrase"
    phrase: str = Field(min_length=1)
    source_field: str | None = None


class NonRankableProfileFieldBlocker(BaseModel):
    """Sort context asked for a profile field that is not in the rank-fields enum.

    The catalog says the phrase IS an NCES profile field, but only the
    rank-fields subset is governed for ordering districts. The validator
    surfaces this as a clarify route with the rankable alternatives.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["non_rankable_profile_field"] = "non_rankable_profile_field"
    phrase: str = Field(min_length=1)
    source_field: str | None = None
    resolved_field_key: str = Field(min_length=1)
    rankable_candidates: tuple[ResolvedEntity, ...] = Field(default_factory=tuple)


class AmbiguousDistrictBlocker(BaseModel):
    """Multiple covered districts match the phrase.

    The validator turns this into a clarify with real candidates from
    the catalog so the user can pick (e.g. "Philadelphia, PA" vs
    "Philadelphia, MS").
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["ambiguous_district"] = "ambiguous_district"
    phrase: str = Field(min_length=1)
    source_field: str | None = None
    candidates: tuple[ResolvedEntity, ...] = Field(default_factory=tuple)


FinalizerBlocker = Annotated[
    UnsupportedConceptBlocker
    | UnresolvedPhraseBlocker
    | NonRankableProfileFieldBlocker
    | AmbiguousDistrictBlocker,
    Field(discriminator="kind"),
]
"""Typed reason the finalizer cannot produce a clean execute plan.

Each variant is the input the step-5 shape validator needs to choose
the right route: ``unsupported_concept`` → refuse, ``unresolved_phrase``
/ ``non_rankable_profile_field`` / ``ambiguous_district`` → clarify
with real candidates pulled from the catalog rather than from prose
rules.
"""


class FinalizedPlan(BaseModel):
    """Output of :func:`finalize_plan`.

    ``plan`` is the slot-corrected :class:`QueryPlan`: legacy
    :class:`SortSpec` collapsed into :class:`SortStepSpec`, metrics ↔
    profile_fields rebalanced based on reconciler evidence, sort-step
    key_types corrected. The shape mirrors the existing planner output
    so the validator and executor consume it without changes.

    ``blockers`` lists the typed reasons (if any) the finalizer can't
    produce a clean execute plan. Step 5's validator dispatches by
    blocker kind.

    ``slot_moves`` is the audit trail. Step-6 retire-the-workarounds
    review filters this by ``kind`` to count how often the finalizer
    actually exercises each transformation path.
    """

    model_config = ConfigDict(extra="forbid")

    plan: QueryPlan
    blockers: tuple[FinalizerBlocker, ...] = Field(default_factory=tuple)
    slot_moves: tuple[SlotMove, ...] = Field(default_factory=tuple)

    @property
    def deferred_metric_gap(self) -> tuple[str, ...]:
        """#1613 U4: requested metric phrases that have no catalog metric
        (``confidence="none"``) and were stripped from ``plan.metrics`` so the
        resolvable subset of a multi-metric lookup can execute.

        Stored on :attr:`plan` (``QueryPlan.deferred_metric_gap``), not here,
        because only ``finalized.plan`` flows downstream to execution — this is
        the finalizer-output view of that same value. Empty unless a multi-metric
        lookup answered a subset and deferred the unresolvable metric(s) as an
        honest gap; the executor copies these onto
        ``ResultSelection.unresolved_metrics`` for the renderer to disclose.
        Stripping these is what makes :attr:`is_ready` True — the gap is
        disclosed, never silently dropped."""

        return self.plan.deferred_metric_gap

    @property
    def is_ready(self) -> bool:
        """True when the finalized plan has no blockers."""

        return not self.blockers
