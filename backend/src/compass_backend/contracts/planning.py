"""Typed planning contracts for the fresh Compass pipeline."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from compass_backend.contracts.recognition import PlanningRecognitionMention
from compass_backend.policy_guidance.models import GuidanceLayer
from compass_backend.reference import normalize_state as _normalize_state

PlannerRoute = Literal["direct", "clarify", "execute", "policy_guidance", "publication"]
PolicyGuidanceResponseMode = Literal["summary", "exemplar_detail", "advisory_comparison"]
DistrictScope = Literal[
    "named_districts",
    "largest_districts",
    "all_covered_districts",
    "state",
    "unspecified",
]
MissingField = Literal[
    "district",
    "metric",
    "scope",
    "time_period",
    "comparison_group",
    "output_format",
    "profile_field",
]
MetricRole = Literal["primary", "comparison", "filter", "grouping"]
DegreeLane = Literal["ba", "ma"]
FilterOperator = Literal[
    "equals",
    "not_equals",
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
    "in",
    "not_in",
    "contains",
    "is_missing",
    "is_not_missing",
]
FilterValue = str | int | float | bool | list[str] | list[int] | list[float]
FilterKind = Literal["state", "region", "enrollment", "metric_value"]
SortDirection = Literal["asc", "desc"]
SortNulls = Literal["first", "last"]
SortStepPhase = Literal["selection", "presentation"]
SortKeyType = Literal["policy_metric", "profile_field", "artifact_field", "district"]
LimitKind = Literal["top", "bottom", "first", "all"]
OutputFormat = Literal["text", "table", "chart", "csv_export"]
RowDisplayMode = Literal["preview", "all"]
OutputGroupBy = Literal["none", "state"]
QueryOperation = Literal[
    "rank",
    "lookup",
    "count",
    "trend",
    "profile_lookup",
    "peer_comparison",
    "similarity",
]
CountKind = Literal[
    "threshold_count",
    "covered_universe_count",
    "categorical_value_count",
    "topic_coverage_count",
]
ContextSelectionMode = Literal[
    "prior_query", "prior_result_rows", "prior_result_population"
]
TemporalIntent = Literal[
    "current",
    "specific_year",
    "year_range",
    "relative_trend_window",
    "comparison_years",
    "latest_available",
]


class DirectResponse(BaseModel):
    """Planner route for requests that do not need data execution."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    reason: str = Field(
        min_length=1,
        description="Brief reason this turn can be answered directly.",
    )


class ClarificationOption(BaseModel):
    """One grounded, clickable answer choice for a clarification turn.

    Born-from-data: each option is derived deterministically from a real
    catalog candidate (e.g. a covered ``DistrictCandidate``), never invented.
    ``value`` is the machine handle a click sends back (for districts, the
    ``district_id`` as a string); ``label`` is the human-readable choice;
    ``detail`` is an optional secondary line (state, enrollment, locale).

    Default-empty on ``ClarificationRequest`` so free text stays first-class
    and every existing clarify turn is unaffected. (#1348)
    """

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1)
    label: str = Field(min_length=1)
    detail: str | None = None


class ClarificationRequest(BaseModel):
    """Planner route for turns that need more user input."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    missing_fields: list[MissingField] = Field(min_length=1)
    candidates: list[str] = Field(default_factory=list)
    # #1348: structured, clickable counterparts to the prose ``candidates``.
    # Born deterministically from grounded catalog candidates at the site that
    # builds this request (e.g. execution.selection's district-ambiguity
    # branch). Default-empty keeps the prose path and every existing clarify
    # turn unchanged; ``candidates`` stays load-bearing for the metric path.
    candidate_options: list[ClarificationOption] = Field(default_factory=list)
    pending_context: PendingQueryContext | None = Field(default=None, exclude=True)
    # W1-08 (#848): typed marker for "this clarify turn was emitted by the
    # planner-output-validation safe-structure fallback path." Lets the
    # rescue-enrichment guard detect the case without comparing prose
    # strings to a verbatim constant — change the rescue wording and the
    # marker survives. Default False keeps every other clarify turn
    # unaffected.
    #
    # TRANSIENT, by design: the shape-guard enrichment stage
    # (_enrich_rescue_with_prior_context) consumes this flag and REBUILDS the
    # turn with is_rescue_fallback=False, so the gate
    # (_rescue_clarification_can_be_enriched) does not re-fire and loop. So a
    # rescue clarify that reaches the persisted turn carries
    # is_rescue_fallback=False — do NOT use this field to detect "was this an
    # over-clarify" after the fact; use ``rescue_origin`` below.
    is_rescue_fallback: bool = False
    # #1613: DURABLE origin marker — "this clarify turn originated from the
    # rescue-fallback path (raw planner-output failure OR its shape-guard
    # enrichment)." Set True at BOTH rescue sites
    # (orchestration/structured_output_failure.py and the enrichment rebuild in
    # orchestration/chat.py) and NEVER cleared, so unlike ``is_rescue_fallback``
    # it survives to the persisted/returned turn. No control-flow code reads it
    # (the enrichment gate keeps reading ``is_rescue_fallback``), so it is
    # observability-only: the answerability_rescue_fallback_is_failure eval
    # criterion reads it to score over-clarify as a verdict failure. Default
    # False keeps every genuine planner clarify unaffected.
    rescue_origin: bool = False


class SelectionSpec(BaseModel):
    """Which district rows the future deterministic executor should select."""

    model_config = ConfigDict(extra="forbid")

    scope: DistrictScope
    districts: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_scope_values(self) -> Self:
        """Keep scope-specific values explicit before execution exists.

        ``state``/``all_covered_districts``/``largest_districts`` each
        materialise their own ``districts`` list from the catalog at
        execution time, so a pre-populated ``districts`` value would be
        silently dropped. ``states`` on those non-``named_districts``
        scopes is a legitimate narrowing filter.

        For #1069's Dallas → Vermont comparison, the planner mixed
        ``scope=state`` with an anchor in ``districts`` and the anchor
        vanished. Rejecting that shape forces the planner-output retry to
        emit ``scope=named_districts`` with every district listed by name.
        """

        if self.scope == "named_districts" and not self.districts:
            raise ValueError("named_districts scope requires districts")
        if self.scope == "state" and not self.states:
            raise ValueError("state scope requires states")
        if self.scope in ("state", "all_covered_districts", "largest_districts") and self.districts:
            raise ValueError(
                f"{self.scope} scope must not set districts; use "
                "scope=named_districts to list anchor + comparison "
                "districts together"
            )
        return self


class MetricSpec(BaseModel):
    """Metric requested by the user before catalog resolution exists."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    role: MetricRole = "primary"
    degree_lane: DegreeLane | None = Field(
        default=None,
        description=(
            "Degree lane for salary metrics. Set 'ba' for bachelor's/BA and "
            "'ma' for master's/MA only when the user explicitly names the lane. "
            "For salary MetricSpecs where the user does not name a lane, leave "
            "None and do not silently default; the catalog applies the reviewed "
            "BA default with a renderer disclosure. Leave None for non-salary "
            "metrics (workdays, observations, etc.) where lane is meaningless."
        ),
    )


class ProfileFieldSpec(BaseModel):
    """NCES/profile field requested before catalog resolution."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)


class AnchorValueSpec(BaseModel):
    """Use one district's reviewed metric value as a filter value."""

    model_config = ConfigDict(extra="forbid")

    district: str = Field(min_length=1)
    state: str | None = Field(
        default=None,
        description=(
            "Optional 2-letter state qualifier for the anchor district, to "
            "disambiguate same-name districts (e.g. Portland ME vs OR). Prefer "
            "this over bundling the state into `district` ('Portland ME'); the "
            "executor still splits a bundled suffix as a fallback. Full names "
            "('Maine') normalize to the abbreviation; unrecognized values are "
            "rejected — mirrors the exclude_states validator."
        ),
    )
    metric: str | None = None
    exclude_anchor: bool = True

    @field_validator("state")
    @classmethod
    def _normalize_anchor_state(cls, value: str | None) -> str | None:
        if value is None:
            return None
        result = _normalize_state(value)
        if len(result) != 2:
            raise ValueError(
                f"AnchorValueSpec.state {value!r} could not be normalized to a "
                f"2-letter state abbreviation (got {result!r}). Use a 2-letter "
                "abbreviation or full state name such as 'ME', 'CA', 'Maine'."
            )
        return result


class FilterSpec(BaseModel):
    """A deterministic filter requested by the planner.

    ``kind`` (#1373) is the typed classification — one of ``state`` / ``region``
    / ``enrollment`` / ``metric_value`` — naming what this filter constrains. The
    planner sets it directly (see ``planner.md``); execution reads it via
    :func:`execution.filters.filter_kind` instead of re-deriving the decision
    from string parsing of ``field``. It is optional and back-compat — when the
    planner omits it, ``filter_kind`` derives the kind from ``field`` exactly as
    before.

    ``field`` carries the data each kind needs, and stays free-form for the kind
    that needs it:
    - ``kind="state"`` — ``field="state"``; the 2-letter code(s) live in ``value``.
    - ``kind="region"`` — ``field="region"``; the region phrase lives in ``value``.
    - ``kind="enrollment"`` — ``field="enrollment"``; the size bound in ``value``.
    - ``kind="metric_value"`` — ``field`` is the free-form metric phrase, resolved
      via ``CatalogResolver.resolve_primary_metric()`` at runtime. Mirror the same
      phrase used in the corresponding ``MetricSpec.name`` so a single catalog call
      resolves both. ``kind`` types only the *decision*, never the phrase.

    The legacy ``field="value"`` token is still supported for back-compat with
    count plans that pre-date the ``kind`` discriminator.

    ``threshold_hint`` is set when the user expressed the threshold via a
    vague quantifier ("regularly", "a couple", "real bonuses") rather than
    a concrete number. The planner picks the concrete ``value`` AND records
    the user's phrase here. The renderer surfaces both: the chosen number
    answers the question; the hint shows how Compass interpreted the prose.
    Leave ``None`` when the user gave a concrete number (">190", ">$50K", etc).
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    operator: FilterOperator
    value: FilterValue | None = None
    anchor_value: AnchorValueSpec | None = None
    kind: FilterKind | None = Field(
        default=None,
        description=(
            "Optional typed classification of this filter (#1373). When set, "
            "execution trusts it instead of re-deriving the kind from `field` "
            "string parsing: 'state'/'region'/'enrollment' are selection-phase "
            "constraints; 'metric_value' is a catalog-resolved metric threshold "
            "(keep the metric phrase in `field`). Omit it (None) to let the "
            "executor derive the kind from `field` exactly as before."
        ),
    )
    threshold_hint: str | None = Field(
        default=None,
        max_length=80,
        description=(
            "Optional user-prose qualifier for vague-quantifier filters. "
            "Set when the user expressed the threshold via a phrase rather "
            "than a number ('regularly', 'a couple', 'real bonuses'). "
            "Leave None when the user gave a concrete number. The renderer "
            "surfaces this in response prose so the user sees how their "
            "vague phrase was interpreted."
        ),
    )

    @field_validator("threshold_hint")
    @classmethod
    def _strip_hint(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                "threshold_hint must not be empty after stripping whitespace"
            )
        return stripped

    @model_validator(mode="after")
    def _check_kind_consistency(self) -> Self:
        """Enforce the invariants a planner-set ``kind`` implies (#1373 Stage 2).

        Only runs when ``kind`` is set; an unset ``kind`` defers entirely to
        :func:`execution.filters.filter_kind`'s field-derivation (back-compat).
        Uses only this model's own fields — never the ``field`` string — so the
        dual-read precedence (a planner-set ``kind`` overrides field-derivation)
        is preserved.

        The planner runs with ``retries={"output":0}``, so a raised error
        surfaces as a rescue clarification rather than a silent re-roll; these
        rules therefore reject only shapes the planner never legitimately emits.
        """
        if self.kind is None:
            return self
        if self.kind in ("state", "region", "enrollment") and (
            self.anchor_value is not None
        ):
            raise ValueError(
                f"FilterSpec.kind={self.kind!r} is a selection-phase constraint "
                "and cannot carry an anchor_value; anchor values compare against a "
                "metric value, which is kind='metric_value'."
            )
        if (
            self.kind == "metric_value"
            and self.value is None
            and self.anchor_value is None
            and self.operator not in ("is_missing", "is_not_missing")
        ):
            raise ValueError(
                "FilterSpec.kind='metric_value' with operator "
                f"{self.operator!r} needs a value or anchor_value to compare "
                "against; otherwise it would filter nothing and silently return "
                "every district."
            )
        return self


class SortSpec(BaseModel):
    """A deterministic sort requested by the planner."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    direction: SortDirection = "asc"
    nulls: SortNulls = "last"


class LimitSpec(BaseModel):
    """A deterministic row limit requested by the planner.

    For kind='all', count must be None — the executor returns every matching
    row without a cap. For all other kinds, count must be a positive integer.
    """

    model_config = ConfigDict(extra="forbid")

    count: int | None = Field(default=None, gt=0)
    kind: LimitKind = "first"

    @model_validator(mode="after")
    def _check_count_for_kind(self) -> "LimitSpec":
        if self.kind == "all":
            if self.count is not None:
                raise ValueError("count must be None for kind='all'")
        elif self.count is None:
            raise ValueError(f"count required for kind={self.kind!r}")
        return self


class SortStepSpec(BaseModel):
    """One ordered deterministic sort/limit step."""

    model_config = ConfigDict(extra="forbid")

    phase: SortStepPhase = "presentation"
    field: str = Field(min_length=1)
    direction: SortDirection = "asc"
    nulls: SortNulls = "last"
    key_type: SortKeyType = "policy_metric"
    limit: LimitSpec | None = None
    defaulted: bool = False


class OutputSpec(BaseModel):
    """Requested output shape before rendering exists."""

    model_config = ConfigDict(extra="forbid")

    format: OutputFormat = "text"
    include_citations: bool = True
    row_display: RowDisplayMode = "preview"
    group_by: OutputGroupBy = "none"


class TemporalSpec(BaseModel):
    """Internal academic-year intent trusted by deterministic execution."""

    model_config = ConfigDict(extra="forbid")

    intent: TemporalIntent = "current"
    academic_year: str | None = None
    start_academic_year: str | None = None
    end_academic_year: str | None = None
    academic_years: list[str] = Field(default_factory=list)
    window_years: int | None = Field(default=None, ge=1)


SimilarityFeatureSet = Literal["enrollment", "frpl", "locale", "all"]


def _normalize_exclude_states_list(values: object) -> list[str]:
    """Normalize each entry to a 2-letter uppercase state abbreviation.

    Shared by ``SimilarityQuerySpec`` and ``PeerComparisonOverrides`` validators.
    ``normalize_state`` maps full state names (e.g. "California") to their
    abbreviation ("CA") and upper-cases anything it cannot map. Values that
    are not exactly 2 characters after normalization are rejected with a
    ``ValueError`` so the planner is forced to correct them rather than
    silently no-op-ping (e.g. ``["California"]`` → ValueError; ``["CA"]``
    → ``["CA"]``).
    """

    if not isinstance(values, list):
        return values  # type: ignore[return-value]  # Pydantic will validate type
    normalized: list[str] = []
    for entry in values:
        if not isinstance(entry, str):
            raise ValueError(
                f"exclude_states entries must be strings, got {type(entry).__name__!r}"
            )
        result = _normalize_state(entry)
        if len(result) != 2:
            raise ValueError(
                f"exclude_states entry {entry!r} could not be normalized to a "
                f"2-letter state abbreviation (got {result!r}). "
                "Use 2-letter abbreviations such as 'CA', 'TX', 'NY'."
            )
        normalized.append(result)
    return normalized


class SimilarityQuerySpec(BaseModel):
    """Typed peer-set discovery specification for ``operation='similarity'``.

    Anchor is identified by human-readable name and resolved by the executor
    via ``CatalogResolver.resolve_peer_set`` (same PR 2A pattern as
    ``peer_comparison``). ``feature_set`` biases the NCES scoring weights
    toward a subset of factors; ``exclude_states`` removes candidates before
    scoring so the peer set excludes districts from those states.
    """

    model_config = ConfigDict(extra="forbid")

    anchor_name: str = Field(
        min_length=1,
        description="Human-readable anchor district name resolved at executor time.",
    )
    feature_set: SimilarityFeatureSet = Field(
        default="all",
        description=(
            "Which NCES factor group to bias toward. 'all' uses the full "
            "governed policy weights. Other values zero out excluded factors "
            "and renormalize so the weighted sum remains meaningful."
        ),
    )
    exclude_states: list[str] = Field(
        default_factory=list,
        description=(
            "Two-letter state abbreviations to exclude from the peer candidate "
            "pool before scoring. Pre-scoring exclusion means these districts "
            "are never ranked, not merely filtered post-scoring."
        ),
    )
    limit: int = Field(
        default=10,
        gt=0,
        le=50,
        description="Maximum number of peer districts to return.",
    )

    @field_validator("exclude_states", mode="before")
    @classmethod
    def normalize_exclude_states(cls, values: object) -> list[str]:
        return _normalize_exclude_states_list(values)


class PeerComparisonOverrides(BaseModel):
    """Optional peer-scoring biases for ``operation='peer_comparison'``.

    Mirrors ``SimilarityQuerySpec``'s ``feature_set`` and ``exclude_states``
    semantics so the metric-bearing peer-comparison path can escape the default
    geography-weighted scoring (C3 narrowed in Phase 0B). Anchor and limit
    come from ``selection.districts[0]`` / ``plan.limit`` — no redundant fields
    here.

    Default ``None`` on ``QueryPlan.peer_overrides`` means no override →
    identical legacy behaviour.
    """

    model_config = ConfigDict(extra="forbid")

    feature_set: SimilarityFeatureSet = Field(
        default="all",
        description=(
            "Which NCES factor group to bias toward. 'all' uses the full "
            "governed policy weights. Other values zero out excluded factors "
            "and renormalize so the weighted sum remains meaningful."
        ),
    )
    exclude_states: list[str] = Field(
        default_factory=list,
        description=(
            "Two-letter state abbreviations to exclude from the peer candidate "
            "pool before scoring. Pre-scoring exclusion means these districts "
            "are never ranked, not merely filtered post-scoring."
        ),
    )

    @field_validator("exclude_states", mode="before")
    @classmethod
    def normalize_exclude_states(cls, values: object) -> list[str]:
        return _normalize_exclude_states_list(values)


class PendingQueryContext(BaseModel):
    """Typed partial query memory while Compass is still clarifying a request."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _strip_anchor_districts_already_grounded(cls, data):
        """#1028: derive-recover a redundant anchor in an anchor-value clarify.

        When clarifying a "same value as [anchor]" request, the planner
        sometimes keeps the correct ``scope=all_covered_districts`` but also
        leaks the anchor district into ``selection.districts`` while already
        grounding that district in a sibling ``FilterSpec.anchor_value``. The
        ``districts`` list is then redundant, but
        ``SelectionSpec.require_scope_values`` rejects it (all_covered_districts
        must not set districts) → the turn collapses to the canned rescue.

        Strip the redundant districts using TYPED fields only (never the user
        prose, guardrail 4), and ONLY when EVERY listed district is already
        grounded by a sibling ``anchor_value.district`` — so the anchor never
        vanishes (#1069 guard stays intact) and no row set changes. When no
        such filter grounds them, this is a no-op and the SelectionSpec
        validator still rejects, which is correct: the anchor would otherwise
        disappear. Surface-scoped: the identical SelectionSpec validator stays
        strict on ``QueryPlan.selection``.
        """
        if not isinstance(data, dict):
            return data
        sel = data.get("selection")
        if not (
            isinstance(sel, dict)
            and sel.get("scope") == "all_covered_districts"
            and sel.get("districts")
        ):
            return data
        grounded = {
            str(f["anchor_value"]["district"]).strip().casefold()
            for f in (data.get("filters") or [])
            if isinstance(f, dict)
            and isinstance(f.get("anchor_value"), dict)
            and f["anchor_value"].get("district")
        }
        listed = [str(d).strip() for d in sel["districts"]]
        if listed and all(d.casefold() in grounded for d in listed):
            new_sel = dict(sel)
            new_sel["districts"] = []
            new_data = dict(data)
            new_data["selection"] = new_sel
            return new_data
        return data

    operation: QueryOperation | None = None
    question: str | None = None
    selection: SelectionSpec | None = None
    metrics: list[MetricSpec] = Field(default_factory=list)
    profile_fields: list[ProfileFieldSpec] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    sort: SortSpec | None = None
    # W1-02 (#836): execution-relevant QueryPlan fields that must round-trip
    # across clarify→answer so the promoted plan keeps the typed meaning
    # the planner originally inferred. Pre-W1-02, sort_steps,
    # requires_all_metrics, count_kind, and inherit_selection_from were
    # dropped at promotion time — a valid two-step request could lose its
    # multi-step sort, an intersection prompt could collapse to a union, a
    # covered-universe count could become a threshold_count, and an
    # explicit "use those districts" selection could fall back to a generic
    # scope. The fields default to QueryPlan's defaults so an unaware
    # caller behaves the same as before.
    sort_steps: list[SortStepSpec] = Field(default_factory=list)
    limit: LimitSpec | None = None
    inherit_selection_from: ContextSelectionMode | None = None
    output: OutputSpec | None = None
    temporal: TemporalSpec | None = None
    requires_all_metrics: bool = Field(default=False)
    requires_composite_ranking: bool = Field(
        default=False,
        description=(
            "W2-M3-01 phase 2 (#857): the user accepted the group in an N-metric "
            "clarification (\"do all\", \"all of them\", \"all N separately\"). "
            "The promoted ``QueryPlan`` must keep every metric and tell the "
            "executor to emit a ``CompositeRankingResult`` envelope instead of a "
            "single ``MetricRankingResult``. Pending shapes don't yet enforce "
            "the 2..8 metric bound — that's a ``QueryPlan`` validator so partial "
            "shapes mid-clarification can still carry the flag."
        ),
    )
    count_kind: CountKind = Field(default="threshold_count")
    missing_fields: list[MissingField] = Field(default_factory=list)
    last_clarification_question: str | None = None
    source_turn_index: int | None = Field(default=None, ge=1)
    similarity: SimilarityQuerySpec | None = Field(
        default=None,
        description=(
            "Typed similarity-discovery spec carried through clarification "
            "so Phase 3 Track 3.1 follow-ups can reference the anchor and "
            "feature_set without re-reading user prose."
        ),
    )
    peer_overrides: PeerComparisonOverrides | None = Field(
        default=None,
        description=(
            "Optional peer-scoring biases carried through clarification "
            "for operation='peer_comparison' follow-ups (Track 3.2)."
        ),
    )

    @field_validator("selection", mode="before")
    @classmethod
    def degenerate_named_districts_selection_means_none(cls, value: object) -> object:
        """Map the planner's "district still unknown" encoding to ``None``.

        In pending context, ``named_districts`` with no districts is the
        planner saying "the district is what I'm asking about" — partial query
        memory mid-clarification, whose legal typed representation is
        ``selection=None``. Without this mapping, ``SelectionSpec``'s #1069
        anchor-vanishing guard (correct for ``QueryPlan.selection``) rejects
        the planner's natural encoding and — with planner output retries at 0 —
        discards an otherwise-correct clarify turn for the generic rescue
        (#1513, case 1159 "Portland"). Deliberately narrow: only the exact
        degenerate raw-dict encoding (scope ``named_districts``, ``districts``
        empty or absent, no states, no extra keys) maps to ``None``; every
        other shape passes through untouched so invalid shapes still fail
        loudly, and ``QueryPlan.selection`` strictness is unchanged. Dict
        input only: a ``SelectionSpec`` *instance* validates at construction,
        so it can never be degenerate. The literal key set mirrors
        ``SelectionSpec``'s fields on purpose: if ``SelectionSpec`` grows a
        field this validator doesn't know, a degenerate dump carrying it
        fails loudly (fail-closed) rather than being silently absorbed.
        """

        if (
            isinstance(value, dict)
            and value.get("scope") == "named_districts"
            and not value.get("districts")
            and not value.get("states")
            and not (value.keys() - {"scope", "districts", "states"})
        ):
            return None
        return value


class QueryPlan(BaseModel):
    """Planner route for turns that should later execute deterministically."""

    model_config = ConfigDict(extra="forbid")

    inherit_from_session: bool = False
    operation: QueryOperation = "rank"
    question: str = Field(min_length=1)
    selection: SelectionSpec
    metrics: list[MetricSpec] = Field(default_factory=list)
    profile_fields: list[ProfileFieldSpec] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    sort: SortSpec | None = None
    sort_steps: list[SortStepSpec] = Field(default_factory=list)
    limit: LimitSpec | None = None
    inherit_selection_from: ContextSelectionMode | None = None
    output: OutputSpec = Field(default_factory=OutputSpec)
    temporal: TemporalSpec = Field(default_factory=TemporalSpec)
    requires_all_metrics: bool = Field(
        default=False,
        description=(
            "When true, deterministic execution returns rows that satisfy EVERY "
            "listed metric (AND-intersection) rather than the union. Set this for "
            "'both X and Y' / 'combine X with Y' / 'link X to Y' intersection "
            "prompts. Only applies to operation='lookup' with 2+ metrics."
        ),
    )
    requires_composite_ranking: bool = Field(
        default=False,
        description=(
            "W2-M3-01 phase 2 (#857): emit one ranking table per metric (a "
            "``CompositeRankingResult`` envelope) instead of a single combined "
            "ranking. Set this when the user accepted the group in an N-metric "
            "clarification (\"do all\", \"all of them\", \"all N separately\"). "
            "Only valid for ``operation='rank'`` with 2..8 metrics, matching "
            "``CompositeRankingResult``'s bounds."
        ),
    )
    deferred_metric_gap: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "#1613 U4: requested metric phrases that have NO catalog metric "
            "(``confidence='none'``) and were stripped from ``metrics`` by the "
            "finalizer so the resolvable subset of a multi-metric lookup can "
            "execute. FINALIZER-SET, never planner-authored — the planner always "
            "leaves this empty; ``finalize_plan`` populates it when it answers a "
            "subset and defers the unresolvable metric(s) as an honest gap. It "
            "rides the QueryPlan (not FinalizedPlan) because only ``finalized.plan`` "
            "flows to execution; the lookup executor copies it onto "
            "``ResultSelection.unresolved_metrics`` for the renderer to disclose. "
            "Empty for every fully-resolvable plan."
        ),
    )
    count_kind: CountKind = Field(
        default="threshold_count",
        description=(
            "Which count shape the planner wants when operation='count'. "
            "'threshold_count' (default) counts districts whose answer satisfies "
            "a metric/filter ('how many districts offer X?'). "
            "'covered_universe_count' counts the covered-district universe size "
            "with no metric ('how many districts do you cover?'). "
            "'categorical_value_count' produces a per-value breakdown across a "
            "grouping metric ('breakdown of sick-leave policy types'). "
            "'topic_coverage_count' counts how many covered districts have ANY "
            "reviewed data in a TOPIC ('how many districts have data on "
            "evaluation policies?'); the planner emits the topic phrase as a "
            "single MetricSpec(name=<topic>) and execution resolves it to that "
            "topic's metrics. "
            "Only meaningful when operation='count'."
        ),
    )
    similarity: SimilarityQuerySpec | None = Field(
        default=None,
        description=(
            "Peer-set discovery specification. Required when "
            "``operation='similarity'``, must be ``None`` for all other "
            "operations. The ``anchor_name`` is resolved by the executor "
            "at runtime via ``CatalogResolver.resolve_peer_set``."
        ),
    )
    peer_overrides: PeerComparisonOverrides | None = Field(
        default=None,
        description=(
            "Optional peer-scoring biases for ``operation='peer_comparison'``. "
            "Set ``feature_set`` to bias NCES weights toward a factor subset; "
            "set ``exclude_states`` to remove candidates before scoring. "
            "Must be ``None`` for all other operations. "
            "Anchor and limit come from ``selection.districts[0]`` / ``plan.limit``."
        ),
    )

    @model_validator(mode="after")
    def similarity_payload_matches_operation(self) -> Self:
        """Require ``similarity`` iff ``operation='similarity'``; reject mismatch."""

        if self.operation == "similarity":
            if self.similarity is None:
                raise ValueError(
                    "operation='similarity' requires a `similarity` payload; "
                    "add a SimilarityQuerySpec with at least `anchor_name`."
                )
            # Anchor name must match selection.districts for downstream
            # validators and renderers that read selection.districts directly.
            if (
                self.selection.districts
                and self.similarity.anchor_name.casefold().strip()
                != self.selection.districts[0].casefold().strip()
            ):
                raise ValueError(
                    "similarity.anchor_name must match selection.districts[0] — "
                    f"got {self.similarity.anchor_name!r} vs "
                    f"{self.selection.districts[0]!r}."
                )
        elif self.similarity is not None:
            raise ValueError(
                f"operation={self.operation!r} must not carry a `similarity` "
                "payload — only operation='similarity' uses it."
            )
        return self

    @model_validator(mode="after")
    def peer_overrides_match_operation(self) -> Self:
        """``peer_overrides`` is only valid on ``operation='peer_comparison'``."""

        if self.peer_overrides is not None and self.operation != "peer_comparison":
            raise ValueError(
                f"operation={self.operation!r} must not carry a `peer_overrides` "
                "payload — only operation='peer_comparison' uses it."
            )
        return self

    @model_validator(mode="after")
    def require_execution_authority_or_inheritance(self) -> Self:
        """Require enough execution authority unless this is a follow-up delta."""

        if self.inherit_from_session:
            return self
        if self.selection.scope == "unspecified":
            raise ValueError("non-inherited query plans require a concrete selection")
        if self.operation == "profile_lookup":
            if not self.profile_fields:
                raise ValueError("profile_lookup query plans require a profile field")
            return self
        # Profile-only rankings are normalized after planning into the
        # executable rank shape: a profile-field MetricSpec plus a profile-field
        # sort step. Let the typed plan reach that deterministic normalizer.
        if self.operation == "rank" and self.profile_fields:
            return self
        # covered_universe_count counts the covered-district universe itself
        # and carries no metric; the count_kind_matches_count_operation_shape
        # validator below enforces metrics-must-be-empty for this shape.
        if self.operation == "count" and self.count_kind == "covered_universe_count":
            return self
        # similarity is peer-set discovery — no policy metric is required.
        if self.operation == "similarity":
            return self
        if not self.metrics:
            raise ValueError("non-inherited query plans require at least one metric")
        return self

    @model_validator(mode="after")
    def intersection_requires_lookup_with_two_metrics(self) -> Self:
        """Reject ``requires_all_metrics`` on shapes where intersection is meaningless."""

        if not self.requires_all_metrics:
            return self
        if self.operation != "lookup":
            raise ValueError(
                "requires_all_metrics=True is only valid for operation='lookup'"
            )
        if len(self.metrics) < 2:
            raise ValueError(
                "requires_all_metrics=True requires at least two metrics"
            )
        return self

    @model_validator(mode="after")
    def composite_ranking_requires_rank_with_two_to_eight_metrics(self) -> Self:
        """Reject ``requires_composite_ranking`` on shapes the envelope can't hold.

        ``CompositeRankingResult`` (phase 1, #806/#852) wraps 2..8 child
        ``MetricRankingResult`` artifacts. Anything outside those bounds — wrong
        operation or wrong metric count — is a planner bug, not a runtime
        problem to recover from.
        """

        if not self.requires_composite_ranking:
            return self
        if self.operation != "rank":
            raise ValueError(
                "requires_composite_ranking=True is only valid for "
                f"operation='rank' (got {self.operation!r})"
            )
        metric_count = len(self.metrics)
        if metric_count < 2 or metric_count > 8:
            raise ValueError(
                "requires_composite_ranking=True requires 2..8 metrics "
                f"(got {metric_count}) to match CompositeRankingResult bounds"
            )
        return self

    @model_validator(mode="after")
    def count_kind_matches_count_operation_shape(self) -> Self:
        """Reject ``count_kind`` combinations that don't match the count shape.

        - ``count_kind`` is only meaningful when ``operation='count'`` — a
          non-default value on any other operation indicates planner confusion.
        - ``covered_universe_count`` counts the covered-district universe and
          carries no metric; non-empty ``metrics`` is a contradiction.
        - ``categorical_value_count`` produces a per-value breakdown across a
          grouping metric; without a ``role='grouping'`` metric there is
          nothing to group by.
        """

        if self.operation != "count":
            if self.count_kind != "threshold_count":
                raise ValueError(
                    "count_kind is only meaningful for operation='count'; "
                    "leave it at the default 'threshold_count' for other operations"
                )
            return self
        if self.count_kind == "covered_universe_count" and self.metrics:
            raise ValueError(
                "count_kind='covered_universe_count' counts the covered universe "
                "and takes no metric; leave metrics empty"
            )
        if self.count_kind == "categorical_value_count":
            grouping_metrics = [m for m in self.metrics if m.role == "grouping"]
            if not grouping_metrics:
                raise ValueError(
                    "count_kind='categorical_value_count' requires a metric with "
                    "role='grouping' to determine the breakdown axis"
                )
        return self


def pending_context_from_plan(
    plan: QueryPlan,
    *,
    last_clarification_question: str | None = None,
    source_turn_index: int | None = None,
    missing_fields: list[MissingField] | None = None,
    metrics: list[MetricSpec] | None = None,
) -> PendingQueryContext:
    """Single source for from-plan ``PendingQueryContext`` capture (#1419).

    Every genuine "from-plan" clarification site — the orchestration shape
    guard and validation→clarify pivot, the planner's
    ``pending_context_from_execution_clarification``, and cross_box's
    ambiguous-catalog clarification — builds a ``PendingQueryContext`` by
    copying a ``QueryPlan``'s typed fields so a clarify→answer round-trip can
    promote back to the same executable plan. Routing them all through this one
    builder guarantees the **W1-02 (#836)** execution-relevant fields survive:
    ``sort_steps``, ``inherit_selection_from``, ``requires_all_metrics``,
    ``requires_composite_ranking``, and ``count_kind``. Hand-built copies had
    drifted (chat.py's shape guard and cross_box dropped some of these); this
    function makes the complete carry the default by construction.

    Optional keyword arguments let callers override the bits that don't come
    from the plan: ``metrics`` (clarification candidates may replace the plan's
    metrics — ``None`` means "use ``plan.metrics``"), the captured
    ``last_clarification_question`` / ``source_turn_index``, and the
    ``missing_fields`` the clarification still needs. List-typed plan fields are
    copied with ``list()`` so the pending context never aliases the plan's
    lists.
    """

    return PendingQueryContext(
        operation=plan.operation,
        question=plan.question,
        selection=plan.selection,
        metrics=metrics if metrics is not None else list(plan.metrics),
        profile_fields=list(plan.profile_fields),
        filters=list(plan.filters),
        sort=plan.sort,
        sort_steps=list(plan.sort_steps),
        limit=plan.limit,
        inherit_selection_from=plan.inherit_selection_from,
        output=plan.output,
        temporal=plan.temporal,
        requires_all_metrics=plan.requires_all_metrics,
        requires_composite_ranking=plan.requires_composite_ranking,
        count_kind=plan.count_kind,
        similarity=plan.similarity,
        peer_overrides=plan.peer_overrides,
        last_clarification_question=last_clarification_question,
        source_turn_index=source_turn_index,
        missing_fields=missing_fields or [],
    )


class PolicyGuidancePlan(BaseModel):
    """Planner route for NCTQ-content questions answered from the typed library.

    The policy_guidance route doesn't touch district data — it surfaces
    NCTQ's published stances, research rationales, and exemplary district
    policies for the requested topics. The renderer reads from the typed
    library directly (no LLM authoring) so the LLM can't fabricate content;
    it can only choose ``topic_ids`` and ``layers``.

    At runtime the planner agent factory subclasses this model with
    ``topic_ids`` and ``primary_topic_id`` typed as ``Literal[*all_topic_ids]``
    so the JSON-schema enum makes hallucinated topics unrepresentable. This
    base class keeps the field as ``list[str]`` so non-agent callers (tests,
    contracts consumers) aren't bound to a runtime-built type.
    """

    model_config = ConfigDict(extra="forbid")

    topic_ids: list[str] = Field(min_length=1, max_length=4)
    layers: list[GuidanceLayer] = Field(min_length=1, max_length=3)
    intent_summary: str = Field(
        min_length=1,
        max_length=240,
        description=(
            "One-sentence paraphrase of why these topics + layers were "
            "selected. Used for telemetry, debug surfaces, and as opening "
            "framing in the rendered response."
        ),
    )
    primary_topic_id: str | None = Field(
        default=None,
        description=(
            "Optional ordering hint when topic_ids has multiple entries. "
            "Renderer leads with this topic; layers from other topics follow."
        ),
    )
    focus_terms: list[str] = Field(
        default_factory=list,
        max_length=6,
        description=(
            "Optional subtopic/narrowing terms for broad topics. Used when the "
            "user asks for a subset such as parental leave within leave or "
            "performance pay within differentiated pay. Renderer filters "
            "approved library items by these terms instead of exposing every "
            "item in the broad topic."
        ),
    )
    selected_exemplar_ids: list[str] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "Optional stable exemplar IDs selected from a prior rendered "
            "policy-guidance response. Used for deterministic exemplar-detail "
            "follow-ups; empty means render all matching guidance."
        ),
    )
    response_mode: PolicyGuidanceResponseMode = Field(
        default="summary",
        description=(
            "summary renders the normal guidance response; exemplar_detail "
            "expands selected approved exemplars without switching to data "
            "execution; advisory_comparison frames a weigh-two-priorities "
            "follow-up (e.g. 'should we prioritize pay or benefits?') as a "
            "not-strictly-either/or answer instead of two stacked topic dumps."
        ),
    )
    region: str | None = Field(
        default=None,
        description=(
            "Optional governed regional refinement for exemplar follow-ups, "
            "e.g. when the user says 'narrow that to just districts in the "
            "South'. Pass the region phrase through ('the South', 'the "
            "Midwest', 'the Northeast', 'the West') — do not enumerate states. "
            "The renderer expands it to the governed U.S. Census member states "
            "and keeps only exemplars located there. All four Census regions "
            "are governed; an ungoverned phrase drops to no in-region match. "
            "Preserve topic_ids/layers/focus_terms from the prior turn."
        ),
    )

    @field_validator("region", mode="before")
    @classmethod
    def normalize_region(cls, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("focus_terms", mode="before")
    @classmethod
    def normalize_focus_terms(cls, value: object) -> list[str]:
        if value is None:
            return []
        raw_values = (value,) if isinstance(value, str) else tuple(value)  # type: ignore[arg-type]
        normalized: list[str] = []
        for raw in raw_values:
            term = str(raw).strip().lower()
            if term and term not in normalized:
                normalized.append(term)
        return normalized

    @field_validator("selected_exemplar_ids", mode="before")
    @classmethod
    def normalize_selected_exemplar_ids(cls, value: object) -> list[str]:
        if value is None:
            return []
        raw_values = (value,) if isinstance(value, str) else tuple(value)  # type: ignore[arg-type]
        normalized: list[str] = []
        for raw in raw_values:
            exemplar_id = str(raw).strip()
            if exemplar_id and exemplar_id not in normalized:
                normalized.append(exemplar_id)
        return normalized

    @model_validator(mode="after")
    def primary_topic_in_topics(self) -> Self:
        if self.primary_topic_id is not None and self.primary_topic_id not in self.topic_ids:
            raise ValueError(
                f"primary_topic_id {self.primary_topic_id!r} must appear in "
                f"topic_ids {self.topic_ids!r}"
            )
        return self

    @model_validator(mode="after")
    def detail_mode_requires_exemplars(self) -> Self:
        if (
            self.response_mode == "exemplar_detail" or self.selected_exemplar_ids
        ) and "exemplars" not in self.layers:
            raise ValueError(
                "selected exemplar policy guidance requires layers to include "
                "'exemplars'"
            )
        return self


class PublicationPlan(BaseModel):
    """Typed plan for the ``publication`` route.

    The user asked what NCTQ has *published* about a topic ("what has NCTQ
    published about the four-day school week?"). That is answered from NCTQ's
    published writing in ``compass.nctq_publications`` — not district data and
    not the curated 8-topic policy-guidance library — so the route carries only
    a free-text topic phrase the planner authored. The search and renderer
    ground every cited title/URL in a fetched publication row; the planner never
    names a publication itself.
    """

    model_config = ConfigDict(extra="forbid")

    publication_query: str = Field(
        min_length=1,
        description=(
            "The topic phrase to search NCTQ's published writing for, authored "
            "by the planner from the user's question (e.g. 'four-day school "
            "week'). Free text — never Literal-bound; the search matches it "
            "against publication titles, summaries, and tags."
        ),
    )
    intent_summary: str = Field(
        min_length=1,
        description="One-line restatement of what the user wants to learn.",
    )


class PlannerTurn(BaseModel):
    """Single typed planner decision for one Compass turn."""

    model_config = ConfigDict(extra="forbid")

    route: PlannerRoute
    confidence: float = Field(ge=0.0, le=1.0)
    direct_response: DirectResponse | None = None
    clarification: ClarificationRequest | None = None
    query_plan: QueryPlan | None = None
    policy_guidance: PolicyGuidancePlan | None = None
    publication: PublicationPlan | None = None
    # W1-01 (#834): typed forensic surface for the recognition blockers
    # observed BEFORE this turn was finalized. The planner agent never
    # populates this directly (its run-time output is empty by default);
    # orchestration patches the turn after recognition completes so the
    # blocker set is auditable even when a finalize-mode rerun returns a
    # clean "execute" route. A non-empty list with route == "execute"
    # signals the finalize path mishandled the blockers and the
    # orchestration must force a clarify/refuse turn instead — see the
    # validator below.
    recognition_blockers: list[PlanningRecognitionMention] = Field(default_factory=list)

    @model_validator(mode="after")
    def execute_route_cannot_carry_unresolved_blockers(self) -> Self:
        """Fail closed if execute route carries unresolved recognition blockers.

        W1-01 (#834) custody check: a finalized planner turn that the
        orchestration has annotated with prior recognition blockers cannot
        also claim ``route == "execute"``. The orchestration is responsible
        for converting such a state into a blocker turn via
        ``_planner_turn_from_recognition_blocker``. If we land here with
        both, something silently dropped the blockers.
        """

        if self.route == "execute" and self.recognition_blockers:
            raise ValueError(
                "PlannerTurn.route == 'execute' but recognition_blockers "
                "is non-empty; orchestration must force a blocker turn "
                "(see W1-01)."
            )
        return self

    @model_validator(mode="after")
    def require_matching_route_payload(self) -> Self:
        """Require exactly one payload matching the selected route."""

        payload_by_route = {
            "direct": self.direct_response,
            "clarify": self.clarification,
            "execute": self.query_plan,
            "policy_guidance": self.policy_guidance,
            "publication": self.publication,
        }
        selected_payload = payload_by_route[self.route]
        if selected_payload is None:
            raise ValueError(f"{self.route} route requires its matching payload")

        extra_routes = [
            route
            for route, payload in payload_by_route.items()
            if route != self.route and payload is not None
        ]
        if extra_routes:
            raise ValueError(
                "PlannerTurn allows only the payload matching route; "
                f"unexpected payloads for: {', '.join(extra_routes)}"
            )
        return self
