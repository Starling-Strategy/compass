"""Result artifacts returned by the fresh Compass API.

`ResultSet` is a discriminated union over six concrete variants — one per
`result_type`. Producers construct the variant directly (e.g.
`MetricCountResult(...)`); consumers dispatch with `isinstance` (or `match`
+ `assert_never` for exhaustive dispatch). The `ResultType` literal alias
names the set of valid tags for code that needs to talk about tags without
committing to a variant (e.g. `ResponseManifest.result_type: ResultType | None`).
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from compass_backend.reference.profile_fields import (
    PROFILE_ENROLLMENT_METRIC_ID,
    PROFILE_SENTINEL_METRIC_IDS,
)

from .citations import CitationRef
from .coverage import (
    CoverageFrame,
    CoverageState,
    is_rendered_answer_state,
    is_stale_prior_value_row,
    short_coverage_label,
)

ResultSelectionScope = Literal[
    "all_covered_districts",
    "state",
    "named_districts",
    "largest_districts",
]
ResultType = Literal[
    "metric_ranking",
    "metric_lookup",
    "metric_count",
    "metric_trend",
    "profile_lookup",
    "peer_comparison",
]
PolicyAnswerValue = str | int | float | bool | None
# Mirror of `compass_backend.contracts.planning.CountKind` — the typed
# discriminator the planner sets on `QueryPlan.count_kind` is the same
# Literal as the row-side discriminator on `CountRow` (this module).
# Declared in both places (rather than imported from contracts) because
# the contracts package eagerly imports artifacts via contracts/chat.py,
# which would create a circular import if results.py tried to import
# from contracts/planning. A test in test_count_kind_alias_in_sync asserts
# the two Literal definitions stay in lockstep.
CountKind = Literal[
    "threshold_count",
    "covered_universe_count",
    "categorical_value_count",
    "topic_coverage_count",
]
MethodologyCode = Literal[
    # Citation / source
    "citation_answer_level_preferred_source_fallback",
    # Ranking / profile ordering
    "profile_rank_uses_profile_field",
    # Lookup
    "lookup_default_district_order",
    "ranked_lookup_selection_order",
    # Intersection / lookup criteria
    "intersection_requires_all_criteria",
    "intersection_accepts_any_current_positive_value",
    # Count
    "count_denominator_current_reviewed_rows",
    "covered_universe_selection_count",
    "topic_coverage_count",
    "categorical_count_grouped_current_values",
    "categorical_count_missing_unavailable_separate",
    # Trend
    "trend_chronological_coverage_gaps",
    "trend_deltas_from_artifact_values",
    # Profile lookup
    "profile_lookup_approved_field",
    "profile_lookup_compass_coverage_flag",
    # Peer comparison / similarity
    "peer_selection_nces_profiles",
    "peer_score_method",
    "peer_policy_cells_with_citations",
    "peer_scoring_policy_disclosure",
    "peer_selection_rationale",
    "peer_metric_coverage_screen_applied",
    # Threshold / filter transparency (PR 2A)
    "metric_value_filter_applied",
    # Categorical inclusion/exclusion filter transparency (#1339)
    "categorical_value_filter_applied",
    # Legacy code retained so persisted pre-hard-filter turns still validate.
    "metric_value_filter_not_applied",
    "anchor_value_filter_applied",
    # Similarity / peer-set discovery (PR 2B / Track 3.2)
    "similarity_feature_set_override",
    "similarity_exclude_states_applied",
    "similarity_post_filter_narrowed_peer_set",
    # Degree lane (PR 2C)
    "degree_lane_applied",
    # Metric best-guess disclosure (Fix 4B, #1 refusal family)
    "metric_best_guess_disclosure",
]


class MethodologyRef(BaseModel):
    """One typed methodology disclosure rendered from deterministic artifacts."""

    model_config = ConfigDict(extra="forbid")

    code: MethodologyCode
    metadata: dict[str, str] = Field(default_factory=dict)
    audience: Literal["user", "internal"] = "user"


class SelectedDistrict(BaseModel):
    """One resolved district in a deterministic result selection."""

    model_config = ConfigDict(extra="forbid")

    district_id: int
    district_name: str = Field(min_length=1)
    state: str | None = None


class ResultSelection(BaseModel):
    """Resolved selection metadata used by validators and renderers."""

    model_config = ConfigDict(extra="forbid")

    scope: ResultSelectionScope
    districts: list[SelectedDistrict] = Field(default_factory=list)
    unresolved_districts: list[str] = Field(default_factory=list)
    unresolved_metrics: list[str] = Field(default_factory=list)
    """#1613 U4: requested metric phrases with no catalog metric
    (``confidence="none"``) that the finalizer stripped from the plan so the
    resolvable subset could execute. The renderer discloses these as an honest
    gap (mirrors ``unresolved_districts``); empty unless a multi-metric lookup
    answered a subset and deferred the unresolvable metric(s)."""
    states: list[str] = Field(default_factory=list)


class RankingRow(BaseModel):
    """One ordered row in a metric ranking result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    district_id: int
    district_name: str = Field(min_length=1)
    state: str | None = None
    metric_id: int
    metric_name: str = Field(min_length=1)
    value: PolicyAnswerValue = None
    display_value: str = Field(min_length=1)
    academic_year: str = Field(min_length=1)
    rank: int = Field(ge=1)
    source: Literal["policy_answer", "profile_field", "coverage_state"] = (
        "policy_answer"
    )
    citation_markers: list[int] = Field(default_factory=list)
    coverage_state: CoverageState | None = None
    coverage_display: str | None = None
    coverage_reason: str | None = None
    coverage_qualifier: str | None = None
    coverage_prior_academic_year: str | None = None
    coverage_prior_display_value: str | None = None
    sort_metric_id: int | None = None
    sort_metric_name: str | None = None
    # Human-facing label for the sort field, used by the renderer for the lead
    # and the table header (#1495). ``sort_metric_name`` carries the machine
    # ``field_key`` (e.g. ``"frpl_pct"``) so the sort-proof validator can match
    # the authority's field_key; the renderer must never show that token. When
    # this is None the renderer falls back to ``sort_metric_name`` — the
    # policy-metric ranking path already stores the human metric name there.
    sort_metric_label: str | None = None
    sort_value: float | None = None
    sort_display_value: str | None = None
    sort_academic_year: str | None = None
    sort_source_document: str | None = None
    sort_source_document_type: str | None = None
    sort_source_url: str | None = None
    sort_source_valid_from: str | None = None
    sort_source_valid_to: str | None = None


class MetricValueRow(BaseModel):
    """One selected-district metric value returned by deterministic lookup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    district_id: int | None
    district_name: str = Field(min_length=1)
    state: str | None = None
    metric_id: int
    metric_name: str = Field(min_length=1)
    value: PolicyAnswerValue = None
    display_value: str = Field(min_length=1)
    academic_year: str = Field(min_length=1)
    source: Literal["policy_answer", "coverage_state"] = "policy_answer"
    citation_markers: list[int] = Field(default_factory=list)
    coverage_state: CoverageState | None = None
    coverage_display: str | None = None
    coverage_reason: str | None = None
    coverage_qualifier: str | None = None
    coverage_prior_academic_year: str | None = None
    coverage_prior_display_value: str | None = None
    numeric_value: float | None = None
    delta_value: float | None = None
    delta_percent: float | None = None
    delta_from_academic_year: str | None = None
    criterion_id: str | None = None
    criterion_label: str | None = None
    criterion_satisfied: bool | None = None


class ResultCriterion(BaseModel):
    """One deterministic criterion used to filter an artifact selection."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    metric_ids: list[int] = Field(min_length=1)
    match_mode: Literal["any_positive"] = "any_positive"
    qualifying_district_ids: list[int] = Field(default_factory=list)


class ProfileValueRow(BaseModel):
    """One NCES/profile field value returned by deterministic profile lookup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    district_id: int | None
    district_name: str = Field(min_length=1)
    state: str | None = None
    field_key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    value: PolicyAnswerValue = None
    display_value: str = Field(min_length=1)
    academic_year: str = Field(min_length=1)
    source: Literal["profile_field"] = "profile_field"
    source_label: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    covered_by_compass: bool = False
    metric_id: int = PROFILE_ENROLLMENT_METRIC_ID
    metric_name: str = Field(min_length=1)
    citation_markers: list[int] = Field(default_factory=list)
    coverage_state: CoverageState | None = None
    coverage_display: str | None = None
    coverage_reason: str | None = None
    coverage_qualifier: str | None = None


class PeerComparisonRow(BaseModel):
    """One policy metric cell for an anchor or deterministic peer district."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    district_id: int
    district_name: str = Field(min_length=1)
    state: str | None = None
    metric_id: int
    metric_name: str = Field(min_length=1)
    value: PolicyAnswerValue = None
    display_value: str = Field(min_length=1)
    academic_year: str = Field(min_length=1)
    source: Literal["policy_answer", "coverage_state"] = "policy_answer"
    citation_markers: list[int] = Field(default_factory=list)
    coverage_state: CoverageState | None = None
    coverage_display: str | None = None
    coverage_reason: str | None = None
    coverage_qualifier: str | None = None
    coverage_prior_academic_year: str | None = None
    coverage_prior_display_value: str | None = None
    peer_role: Literal["anchor", "peer"] = "peer"
    peer_rank: int | None = Field(default=None, ge=1)
    peer_score: float | None = Field(default=None, ge=0.0, le=1.0)
    peer_reason: str = ""
    peer_selection_method: str = "nces_similarity"
    peer_enrollment: int | None = Field(default=None, ge=0)
    peer_urbanicity: str | None = None


# ─── CountRow discriminated union ────────────────────────────────────────────


class _CountRowBase(BaseModel):
    """Fields shared across every count-row variant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    district_id: int | None = None
    district_name: str = "Covered districts"
    state: str | None = None
    metric_id: int
    metric_name: str = Field(min_length=1)
    value: int | float | str | None = None
    display_value: str = Field(min_length=1)
    academic_year: str = Field(min_length=1)
    count: int = Field(ge=0)
    denominator: int = Field(ge=0)
    percent: float | None = Field(default=None, ge=0.0, le=100.0)
    filter_statement: str = Field(min_length=1)
    qualifying_district_ids: list[int] = Field(default_factory=list)
    # The selected POPULATION the count was taken over (covered districts with a
    # current value — the denominator's members), distinct from the matching
    # subset in ``qualifying_district_ids``. Lets a "who are the <denominator>?"
    # follow-up present the population without re-querying (#393).
    denominator_district_ids: list[int] = Field(default_factory=list)
    source: Literal["policy_answer", "coverage_state"] = "policy_answer"
    citation_markers: list[int] = Field(default_factory=list)
    coverage_state: CoverageState | None = None
    coverage_display: str | None = None
    coverage_reason: str | None = None


class ThresholdCountRow(_CountRowBase):
    """Counts districts that meet a numeric / boolean policy threshold."""

    count_kind: Literal["threshold_count"] = "threshold_count"


class CoveredUniverseCountRow(_CountRowBase):
    """Counts the full covered-districts universe (denominator-only result)."""

    count_kind: Literal["covered_universe_count"] = "covered_universe_count"


class CategoricalCountRow(_CountRowBase):
    """One bucket in a categorical-value distribution (one row per category).

    `category` is required on this variant — the producer in execution.count
    always sets it from the bucket key. Trying to access `row.category` on a
    ThresholdCountRow or CoveredUniverseCountRow is a type error: the field
    only exists on this variant.
    """

    count_kind: Literal["categorical_value_count"] = "categorical_value_count"
    category: str = Field(min_length=1)


CountRow = Annotated[
    ThresholdCountRow | CoveredUniverseCountRow | CategoricalCountRow,
    Field(discriminator="count_kind"),
]

# isinstance-checkable tuple of count-row variant classes. The Annotated alias
# above is a typing construct, not a runtime class — `isinstance(x, CountRow)`
# raises TypeError. Use this tuple in runtime guards instead.
COUNT_ROW_TYPES: tuple[type, ...] = (
    ThresholdCountRow,
    CoveredUniverseCountRow,
    CategoricalCountRow,
)


class CoverageDisclosure(BaseModel):
    """One selected result cell disclosed outside the primary data table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    district_id: int | None = None
    district_name: str = Field(min_length=1)
    state: str | None = None
    metric_id: int
    metric_name: str = Field(min_length=1)
    academic_year: str = Field(min_length=1)
    coverage_state: CoverageState
    display: str = Field(min_length=1)
    reason: str | None = None
    qualifier: str | None = None
    prior_academic_year: str | None = None
    prior_display_value: str | None = None


class LargestExcludedDistrict(BaseModel):
    """One larger district left out of an "N largest by [metric]" ranking.

    Born from a real selected district (never invented): when a ranking is
    selected by a size attribute (``largest_districts`` scope) and ranks only
    the districts that carry a comparable numeric value, a *larger* district
    that lacks that value is named here so the answer can disclose it (#1468).
    ``coverage_state`` preserves the honest INA distinction — ``ina``/``na`` are
    reviewed findings (not a gap); ``not_reviewed`` is an actual gap.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    district_id: int
    district_name: str = Field(min_length=1)
    state: str | None = None
    coverage_state: CoverageState


class LargestExcludedDisclosure(BaseModel):
    """Larger districts excluded from an "N largest with current data" ranking.

    Split into two honest buckets per #1468 so the renderer never lumps a
    reviewed finding in with a true data gap:

    - ``reviewed`` — ``ina`` ("issue not addressed") / ``na`` districts NCTQ
      reviewed but that carry no rankable figure. A finding, NOT a gap.
    - ``not_reviewed`` — districts genuinely not yet reviewed for the metric.
      An actual data gap.

    Each bucket lists every excluded district in selection (largest-first)
    order. Naming "up to 3 then (and X more)" is a rendering concern, so the
    artifact keeps the full grounded list and the renderer applies the cap.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewed: list[LargestExcludedDistrict] = Field(default_factory=list)
    not_reviewed: list[LargestExcludedDistrict] = Field(default_factory=list)


class CsvExportArtifact(BaseModel):
    """Artifact-first CSV table payload for frontend/download surfaces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_type: Literal["csv_table"] = "csv_table"
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, object]] = Field(default_factory=list)
    public_columns: list[str] = Field(default_factory=list)
    public_rows: list[dict[str, object]] = Field(default_factory=list)


class ChartPoint(BaseModel):
    """One deterministic chart point sourced from a result row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1)
    value: float
    district_id: int | None = None
    academic_year: str | None = None
    citation_markers: list[int] = Field(default_factory=list)


class ChartSeries(BaseModel):
    """One chart series sourced from result rows for the same metric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1)
    metric_id: int | None = None
    points: list[ChartPoint] = Field(default_factory=list)


class ChartArtifact(BaseModel):
    """Small chart artifact generated only from current covered numeric rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_type: Literal["bar_chart", "line_chart"] = "bar_chart"
    title: str = Field(min_length=1)
    x_axis_label: str = Field(min_length=1)
    y_axis_label: str = Field(min_length=1)
    points: list[ChartPoint] = Field(default_factory=list)
    series: list[ChartSeries] = Field(default_factory=list)
    show_legend: bool = False


# ─── ResultSet discriminated union ───────────────────────────────────────────


class DistrictCoverageSummary(BaseModel):
    """District-level coverage tallies for the narrated availability sentences.

    #1514 D9 grounds the "Of the N districts you asked about, X have current
    reviewed data; Y haven't been reviewed for {year} yet." sentence (and the
    count rule-2 / out-of-Pathfinder mentions) in DISTRICT vocabulary. The
    tallies are stored on the result — not derived at render time — because
    none of them coincide with an existing serialized field: the
    coverage_frame counts cells (district x metric), and on multi-metric
    results the narrated totals would otherwise exist nowhere in the artifact
    JSON, tripping the numeric-token-provenance validator (error severity).

    A district with >= 1 answer cell counts as having current data (the D9
    multi-metric tie-break, flagged to NCTQ in the PR). Districts dedupe by
    ``district_id``; out-of-universe rows have no id and dedupe by name.
    ``requested_academic_year`` carries the year the not-reviewed sentence
    names (from the first not-reviewed row), so the composer reads only this
    summary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    districts_asked: int = Field(ge=0)
    districts_with_current_data: int = Field(ge=0)
    districts_not_reviewed: int = Field(ge=0)
    districts_out_of_universe: int = Field(ge=0)
    requested_academic_year: str | None = None


def district_coverage_summary_for_rows(
    rows: list,
    selection: "ResultSelection | None",
) -> DistrictCoverageSummary:
    """Tally districts (not cells) by coverage from result rows + selection.

    The single artifact-layer authority for district coverage tallies
    (#1514 D9) — the composer reads the stored summary and never re-derives
    it at render time. Dedupe key is ``district_id``; rows without an id
    (out-of-universe names) key by casefolded name, which also dedupes them
    against ``selection.unresolved_districts`` (lookups carry unresolved
    names in both places; counts carry them on the selection only).
    """

    answered: set[tuple[object, ...]] = set()
    gap_states: dict[tuple[object, ...], str] = {}
    requested_year: str | None = None
    for row in rows:
        key: tuple[object, ...] = (
            ("id", row.district_id)
            if row.district_id is not None
            else ("name", row.district_name.casefold())
        )
        state = getattr(row, "coverage_state", None)
        reason = getattr(row, "coverage_reason", None)
        if reason == "unavailable":
            # A rendered "no data" cell (e.g. a profile field with no NCES value
            # for this district) is shown for visibility but is NOT current
            # reviewed data, so record it as a gap rather than counting it as
            # answered — otherwise the availability summary overstates coverage
            # ("3 have current data" while the table shows one as no-data). A
            # district with at least one real answer cell elsewhere still
            # resolves to answered (gaps exclude keys already in ``answered``),
            # so this only down-counts districts whose every cell is a data gap.
            gap_states.setdefault(key, "not_reviewed")
            if requested_year is None:
                requested_year = row.academic_year
            continue
        if is_rendered_answer_state(state):
            answered.add(key)
            continue
        if state == "out_of_universe":
            gap_states[key] = state
        else:
            gap_states.setdefault(key, state)
            if requested_year is None:
                requested_year = row.academic_year
    if selection is not None:
        for name in selection.unresolved_districts:
            gap_states.setdefault(("name", name.casefold()), "out_of_universe")
    gaps = {key: state for key, state in gap_states.items() if key not in answered}
    return DistrictCoverageSummary(
        districts_asked=len(answered | set(gaps)),
        districts_with_current_data=len(answered),
        districts_not_reviewed=sum(
            1 for state in gaps.values() if state != "out_of_universe"
        ),
        districts_out_of_universe=sum(
            1 for state in gaps.values() if state == "out_of_universe"
        ),
        requested_academic_year=requested_year,
    )


def _district_coverage_summary_for_count(
    result: "MetricCountResult",
) -> DistrictCoverageSummary | None:
    """District tallies for a count result, when honestly derivable.

    Categorical buckets carry ``qualifying_district_ids`` per coverage state,
    so the tallies dedupe across buckets here. Threshold / covered-universe
    rows are aggregates whose qualifying ids mean "matched the filter" — they
    do not serialize per-district coverage, so ``build_metric_count_result``
    computes the summary from its per-district label sets and passes it
    explicitly; a hand-built threshold result without one stays ``None`` and
    the composer narrates no district tally rather than a cell count.
    """

    categorical_rows = [
        row for row in result.rows if isinstance(row, CategoricalCountRow)
    ]
    if not categorical_rows:
        return None
    answered: set[int] = set()
    gap_ids: set[int] = set()
    requested_year: str | None = None
    for row in categorical_rows:
        if is_rendered_answer_state(row.coverage_state):
            answered.update(row.qualifying_district_ids)
        else:
            gap_ids.update(row.qualifying_district_ids)
            if requested_year is None:
                requested_year = row.academic_year
    gap_ids -= answered
    out_of_universe = (
        len(result.selection.unresolved_districts)
        if result.selection is not None
        else 0
    )
    return DistrictCoverageSummary(
        districts_asked=len(answered | gap_ids) + out_of_universe,
        districts_with_current_data=len(answered),
        districts_not_reviewed=len(gap_ids),
        districts_out_of_universe=out_of_universe,
        requested_academic_year=requested_year,
    )


class _ResultSetBase(BaseModel):
    """Fields shared across every ResultSet variant.

    Concrete variants subclass this and add a `result_type` Literal
    discriminator plus a row-type-narrowed `rows` field. Per-variant
    `populate_artifact_surfaces` validators populate CSV/chart surfaces
    using only the helpers that make sense for that variant.

    `frozen=True` makes the model faux-immutable after construction so
    a renderer accidentally writing `result.csv_export = ...` raises at
    runtime rather than silently corrupting cross-surface parity. The
    invariant assumed by the surface_consistency validator is "every
    surface derives from the same ResultSet"; freezing locks that in.
    Use `result.model_copy(update={...})` to produce a modified variant.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    selection: ResultSelection | None = None
    citations: list[CitationRef] = Field(default_factory=list)
    coverage_frame: CoverageFrame | None = None
    # #1514 D9 — serialized district tallies behind the narrated availability
    # sentences. Auto-populated (per-variant) for row-shaped variants and
    # categorical counts; threshold-count builders pass it explicitly because
    # aggregate count rows do not serialize per-district coverage. Stays None
    # where it cannot be derived honestly — the composer then narrates nothing
    # rather than a cell count dressed as a district count.
    district_coverage: DistrictCoverageSummary | None = None
    csv_export: CsvExportArtifact | None = None
    chart: ChartArtifact | None = None
    # #1240: an explicit, deliberate "do not build a chart for this result"
    # marker the chart-visibility gate sets when stripping an unrequested chart.
    # The per-variant validators only auto-build when chart is None AND this is
    # False, so the strip survives Pydantic re-validation — assigning a stripped
    # result into any ResultSet-typed field (ChatResponse, TurnSnapshot)
    # re-runs the after-validator, which would otherwise rebuild the chart.
    # Eligibility is unchanged: the normal executor path never sets this flag.
    chart_suppressed: bool = False
    coverage_disclosures: list[CoverageDisclosure] = Field(default_factory=list)
    total_considered: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    order_statement: str = Field(min_length=1)
    source_notes: list[str] = Field(default_factory=list)
    methodology_codes: list[MethodologyRef] = Field(default_factory=list)
    criteria: list[ResultCriterion] = Field(default_factory=list)


# Mutating `self` inside a `mode="after"` validator with `frozen=True` requires
# `object.__setattr__`: Pydantic v2's frozen guard intercepts regular assignment,
# and returning a `model_copy()` from a top-level validator is silently dropped
# during `__init__` (per the v2 warning "Returning anything other than `self`
# from a top level model validator isn't supported when validating via __init__").
# `object.__setattr__` is the documented escape hatch — it bypasses the descriptor
# protocol, which is where the frozen guard lives. After the validator completes,
# the model is fully frozen against any further mutation.


def _assign_csv_export_if_nonempty(
    result: "_ResultSetBase", built: "CsvExportArtifact"
) -> None:
    """Assign a built CSV artifact only when it carries data rows (#1736).

    The CSV's data rows are answer rows only (covered/ina/na via
    ``is_rendered_answer_state``); not_reviewed and out_of_universe rows are
    voiced as narrative sentences, never data points. When every named district
    is not_reviewed for the asked metric, the peer/lookup/profile/trend builders
    filter out every row and append no re-derived disclosure (only ranking does),
    so ``built`` has columns but zero rows — a literally-empty, header-only CSV
    (the #1222 blank-file symptom).

    Suppress that empty artifact: leave ``csv_export`` at its default ``None``.
    ``None`` flips ``has_csv_export`` (computed as ``csv_export is not None`` in
    rendering/writer.py and session/memory.py) to False, so the stylist promises
    no download, SSE ships ``csv_export: None``, the frontend offers no file, and
    the on-screen not_reviewed narrative is the clean message. This is the
    zero-ROW twin of the zero-COLUMN guard in ``_public_csv_export``. Keyed on
    ``built.rows`` (the post-disclosure data-row list present on every builder),
    not ``public_rows`` (the count builder leaves that defaulted).
    """

    if built.rows:
        object.__setattr__(result, "csv_export", built)
    # else: leave csv_export None (the default) — never emit a header-only CSV.


class MetricRankingResult(_ResultSetBase):
    """Ordered ranking of districts on one or more metrics."""

    result_type: Literal["metric_ranking"] = "metric_ranking"
    rows: list[RankingRow] = Field(default_factory=list)
    # #1468: for an "N largest by [metric]" ranking, the larger districts that
    # were left out because they carry no comparable numeric value. None when
    # the rule did not fire (not a largest-scope ranking, or none were
    # excluded). Empty buckets mean "the rule fired but found nothing to name".
    largest_excluded: LargestExcludedDisclosure | None = None

    @model_validator(mode="after")
    def populate_artifact_surfaces(self) -> Self:
        if self.district_coverage is None:
            object.__setattr__(
                self,
                "district_coverage",
                district_coverage_summary_for_rows(self.rows, self.selection),
            )
        if self.csv_export is None:
            # #1736: assign only when the built CSV has data rows.
            _assign_csv_export_if_nonempty(self, _default_csv_export_for_result(self))
        if self.chart is None and not self.chart_suppressed:
            object.__setattr__(self, "chart", _chart_for_ranking(self))
        return self


class ListCoverageSummary(BaseModel):
    """District-level coverage summary for a filtered lookup (#942).

    Grounds the "Of the N {scope}, X (Y%) have current evaluation data" sentence
    so its numbers survive numeric-token provenance — the list analog of
    ``CountRow.percent``. ``district_total`` is the filtered selection size,
    ``district_with_data`` the distinct districts with at least one current
    value, ``percent`` their rounded share. Stored (not derived at render time)
    because none of the three coincide with an existing serialized field — the
    coverage_frame counts cells (district x metric), not districts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    district_total: int = Field(ge=0)
    district_with_data: int = Field(ge=0)
    percent: float | None = Field(default=None, ge=0.0, le=100.0)


class FilterPrevalenceSummary(BaseModel):
    """Pre-narrow prevalence tally for a filtered list answer (#1337 / FILT-88).

    Computed BEFORE ``_apply_metric_value_filters_to_selection`` narrows the
    selection, so the denominator reflects the full universe of districts with a
    real current value — the policy-honest count (#1514 rule 2, COVER-R4).

    - ``matched``: districts that pass the filter (the post-narrow count).
    - ``denominator``: districts in the pre-narrow selection that have a real
      current value for the filter metric (``coverage_state == "covered"``).
      For numeric filters this equals ``len(real_rows)`` in ``build_metric_count_result``
      at the same prompt, providing parity with the count path.
    - ``percent``: matched / denominator × 100, or None when denominator is 0.
    - ``not_reviewed_count``: pre-narrow districts with no row for the filter
      metric (``coverage_state == "not_reviewed"``). Named separately so the
      renderer can disclose them without folding them into the denominator.
    - ``na_count``: pre-narrow districts whose value is "not applicable"
      (``coverage_state == "na"``). Relevant for categorical metrics (e.g. parental
      leave eligibility, where many districts have NA values). Zero for purely
      numeric metrics.

    Supply this field from the executor; ``populate_artifact_surfaces`` on
    ``MetricLookupResult`` does NOT derive it (the whole point is pre-narrow data).
    None when there is no metric-value filter or when multiple filters make a
    single denominator ambiguous.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    matched: int = Field(ge=0)
    denominator: int = Field(ge=0)
    percent: float | None = Field(default=None, ge=0.0, le=100.0)
    not_reviewed_count: int = Field(ge=0)
    na_count: int = Field(ge=0)


class MetricLookupResult(_ResultSetBase):
    """One row per selected district for one metric."""

    result_type: Literal["metric_lookup"] = "metric_lookup"
    rows: list[MetricValueRow] = Field(default_factory=list)
    list_coverage: ListCoverageSummary | None = None
    filter_prevalence: FilterPrevalenceSummary | None = None
    # Name of the metric the rows are ordered by, set by a ranked-lookup
    # executor that orders a multi-metric table by one metric (#1220). When set,
    # the comparison-table renderer marks that column as the ranked one so a
    # reader can tell which figure drives the row order — e.g. a BA-vs-MA salary
    # table sorted by BA pay. None for an unordered lookup (no ranked column).
    ranked_by_metric_name: str | None = None

    @model_validator(mode="after")
    def populate_artifact_surfaces(self) -> Self:
        if self.list_coverage is None and self.selection is not None:
            total = len(self.selection.districts)
            with_data = len(
                {
                    row.district_id
                    for row in self.rows
                    if row.coverage_state == "covered" and row.district_id is not None
                }
            )
            percent = round(with_data / total * 100, 1) if total else None
            object.__setattr__(
                self,
                "list_coverage",
                ListCoverageSummary(
                    district_total=total,
                    district_with_data=with_data,
                    percent=percent,
                ),
            )
        if self.district_coverage is None:
            object.__setattr__(
                self,
                "district_coverage",
                district_coverage_summary_for_rows(self.rows, self.selection),
            )
        if self.csv_export is None:
            # #1736: assign only when the built CSV has data rows.
            _assign_csv_export_if_nonempty(self, _default_csv_export_for_result(self))
        if self.chart is None and not self.chart_suppressed:
            object.__setattr__(self, "chart", _chart_for_metric_lookup(self))
        return self


class MetricCountResult(_ResultSetBase):
    """Aggregate count of districts meeting a condition, per metric or category."""

    result_type: Literal["metric_count"] = "metric_count"
    rows: list[CountRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def populate_artifact_surfaces(self) -> Self:
        if self.district_coverage is None:
            object.__setattr__(
                self,
                "district_coverage",
                _district_coverage_summary_for_count(self),
            )
        if self.csv_export is None:
            # #1736: assign only when the built CSV has data rows.
            _assign_csv_export_if_nonempty(self, _count_csv_export_for_result(self))
        if self.chart is None and not self.chart_suppressed:
            object.__setattr__(self, "chart", _chart_for_metric_count(self))
        return self


class MetricTrendResult(_ResultSetBase):
    """Year-over-year deltas on one metric for selected districts."""

    result_type: Literal["metric_trend"] = "metric_trend"
    rows: list[MetricValueRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def populate_artifact_surfaces(self) -> Self:
        if self.district_coverage is None:
            object.__setattr__(
                self,
                "district_coverage",
                district_coverage_summary_for_rows(self.rows, self.selection),
            )
        if self.csv_export is None:
            # #1736: assign only when the built CSV has data rows.
            _assign_csv_export_if_nonempty(self, _trend_csv_export_for_result(self))
        return self


class ProfileLookupResult(_ResultSetBase):
    """NCES/profile-field values (non-policy data) for selected districts."""

    result_type: Literal["profile_lookup"] = "profile_lookup"
    rows: list[ProfileValueRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def populate_artifact_surfaces(self) -> Self:
        if self.district_coverage is None:
            object.__setattr__(
                self,
                "district_coverage",
                district_coverage_summary_for_rows(self.rows, self.selection),
            )
        if self.csv_export is None:
            # #1736: assign only when the built CSV has data rows.
            _assign_csv_export_if_nonempty(self, _default_csv_export_for_result(self))
        return self


class PeerComparisonResult(_ResultSetBase):
    """Anchor + peer districts side-by-side on policy metrics."""

    result_type: Literal["peer_comparison"] = "peer_comparison"
    rows: list[PeerComparisonRow] = Field(default_factory=list)

    @property
    def is_similarity_result(self) -> bool:
        """Return True when this result carries similarity-discovery rows.

        The typed discriminator: ``build_similarity_result`` sets
        ``metric_id=0`` on every row (the ``_SIMILARITY_SENTINEL_METRIC_ID``
        sentinel). When all rows use the sentinel, the result is a peer-set
        discovery artifact, not a policy-metric comparison — the renderer
        dispatches to ``_render_similarity`` instead of
        ``_render_peer_comparison``.
        """

        return bool(self.rows) and all(row.metric_id == 0 for row in self.rows)

    @model_validator(mode="after")
    def populate_artifact_surfaces(self) -> Self:
        if self.district_coverage is None:
            object.__setattr__(
                self,
                "district_coverage",
                district_coverage_summary_for_rows(self.rows, self.selection),
            )
        if self.csv_export is None:
            # #1736: assign only when the built CSV has data rows.
            _assign_csv_export_if_nonempty(self, _default_csv_export_for_result(self))
        if self.chart is None and not self.chart_suppressed:
            object.__setattr__(self, "chart", _chart_for_metric_lookup(self))
        return self


class CompositeRankingResult(_ResultSetBase):
    """W2-M3-01 (#806): N validated ranking tables in one response envelope.

    Emitted when the user accepts an N-metric clarification with phrasing
    like \"do all 4 separately\" or \"give me one table for each one\" and
    every required scope/sort slot is already known. Each child is a fully
    validated ``MetricRankingResult`` — its own metric, citations, coverage
    labels, methodology, CSV export, and chart. The renderer formats one
    child table at a time and MUST NOT infer the split from free text
    (per the audit-derived rule: typed artifacts, no renderer prose glue).

    The composite is itself a ``ResultSet`` discriminator variant
    (``result_type=\"composite_ranking\"``) so downstream code that switches
    on ``result_type`` handles it explicitly instead of falling through
    a default branch.

    ``rows`` is always empty on the composite envelope itself — every row
    lives on a child. ``total_considered`` and ``excluded_count`` aggregate
    across children for cheap summary numbers; ``order_statement`` is a
    composite-level statement (e.g., \"Four metrics, ranked highest to
    lowest by each.\"); per-child ``order_statement`` carries the per-metric
    detail.
    """

    result_type: Literal["composite_ranking"] = "composite_ranking"
    rows: list = Field(default_factory=list, max_length=0)
    children: list[MetricRankingResult] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def aggregate_summary_fields(self) -> Self:
        """Roll up total_considered + excluded_count from children when 0.

        Authors usually pass ``total_considered=0`` and let the validator
        sum from children rather than hand-calculate. If the caller passes
        a non-zero value we respect it (some scorecard validators set it
        explicitly).
        """

        if self.total_considered == 0 and self.children:
            object.__setattr__(
                self,
                "total_considered",
                sum(child.total_considered for child in self.children),
            )
        if self.excluded_count == 0 and self.children:
            object.__setattr__(
                self,
                "excluded_count",
                sum(child.excluded_count for child in self.children),
            )
        return self

    @model_validator(mode="after")
    def populate_artifact_surfaces(self) -> Self:
        """Aggregate each child's CSV rows into one envelope-level CSV.

        The chat renderer formats one table per child via the per-child
        ``csv_export``; the answer-level *download* needs ONE CSV the
        analyst can open in Excel — a blank ``csv_export`` on the envelope
        was the "CSV was BLANK - no data" bug in #745 (Ashley/Shannon
        sheet rows 62, 64).

        Aggregation policy:
          - One CSV row per child-row, preserving ``metric_name`` as the
            distinguisher.
          - Columns are the same ``_CSV_COLUMNS`` shared by all
            ranking/lookup variants (children are ``MetricRankingResult``).
          - Coverage-disclosure rows from each child are concatenated.

        Chart is intentionally left ``None`` — no obvious aggregation for
        a chart across multiple metrics; the renderer can emit per-child
        charts if needed.
        """

        if self.district_coverage is None and self.children:
            # Aggregate by re-deriving over the concatenated child rows —
            # the same district appears once per child (one child per
            # metric), so summing per-child summaries would double count.
            # The one derivation function dedupes by district identity.
            object.__setattr__(
                self,
                "district_coverage",
                district_coverage_summary_for_rows(
                    [row for child in self.children for row in child.rows],
                    self.selection
                    or (self.children[0].selection if self.children else None),
                ),
            )
        if self.csv_export is None and self.children:
            aggregated_rows: list[dict[str, object]] = []
            for child in self.children:
                if child.csv_export is None:
                    continue
                aggregated_rows.extend(child.csv_export.rows)
            # Derive BOTH public columns and public rows from one fresh
            # projection over the concatenated child rows. The per-column
            # pruning (rule 1/2) now runs per-child, so each child's
            # `public_rows` only carries the keys that survived ITS prune;
            # reusing them here would blank a collapsing child's rank cells
            # whenever a sibling keeps those columns (the union-derived
            # `public_columns` keeps a column that the collapsing child's
            # rows no longer have a key for). Projecting the union once keeps
            # columns and rows consistent and gives every child its real
            # rank values.
            public_columns, public_rows = _public_csv_export(aggregated_rows)
            object.__setattr__(
                self,
                "csv_export",
                CsvExportArtifact(
                    columns=list(_CSV_COLUMNS),
                    rows=aggregated_rows,
                    public_columns=public_columns,
                    public_rows=public_rows,
                ),
            )
        return self


ResultSet = Annotated[
    MetricRankingResult
    | MetricLookupResult
    | MetricCountResult
    | MetricTrendResult
    | ProfileLookupResult
    | PeerComparisonResult
    | CompositeRankingResult,
    Field(discriminator="result_type"),
]

# isinstance-checkable tuple of result-set variant classes. Mirrors the
# EXECUTION_OUTCOME_TYPES pattern in execution/types.py — use this tuple
# in runtime guards; `isinstance(x, ResultSet)` raises TypeError.
RESULT_SET_TYPES: tuple[type, ...] = (
    MetricRankingResult,
    MetricLookupResult,
    MetricCountResult,
    MetricTrendResult,
    ProfileLookupResult,
    PeerComparisonResult,
    CompositeRankingResult,
)


# ─── CSV column constants (unchanged) ────────────────────────────────────────


_CSV_COLUMNS = [
    "district_id",
    "district_name",
    "state",
    "metric_id",
    "metric_name",
    "value",
    "display_value",
    "sort_metric_id",
    "sort_metric_name",
    "sort_value",
    "sort_display_value",
    "sort_academic_year",
    "sort_source_document",
    "sort_source_document_type",
    "sort_source_url",
    "sort_source_valid_from",
    "sort_source_valid_to",
    "criterion_id",
    "criterion_label",
    "criterion_satisfied",
    "academic_year",
    "coverage_state",
    "coverage_display",
    "coverage_reason",
    "coverage_qualifier",
    "coverage_prior_academic_year",
    "coverage_prior_display_value",
    "citation_markers",
    "source_document",
    "source_document_type",
    "source_page",
    "source_url",
    "source_valid_from",
    "source_valid_to",
    "source_urls",
]

_COUNT_CSV_COLUMNS = [
    "metric_id",
    "metric_name",
    "count_kind",
    "category",
    "count",
    "denominator",
    "percent",
    "filter_statement",
    "qualifying_district_ids",
    "source",
    "display_value",
    "academic_year",
    "value",
    "coverage_state",
    "coverage_display",
    "coverage_reason",
    "coverage_qualifier",
    "coverage_prior_academic_year",
    "coverage_prior_display_value",
    "citation_markers",
    "source_document",
    "source_document_type",
    "source_page",
    "source_url",
    "source_valid_from",
    "source_valid_to",
    "source_urls",
]

_TREND_CSV_COLUMNS = [
    "district_id",
    "district_name",
    "state",
    "metric_id",
    "metric_name",
    "value",
    "display_value",
    "numeric_value",
    "academic_year",
    "coverage_state",
    "coverage_display",
    "coverage_reason",
    "coverage_qualifier",
    "coverage_prior_academic_year",
    "coverage_prior_display_value",
    "delta_value",
    "delta_percent",
    "delta_from_academic_year",
    "citation_markers",
    "source_document",
    "source_page",
    "source_url",
    "source_valid_from",
    "source_valid_to",
    "source_urls",
]

_PROFILE_SORT_METRIC_IDS = PROFILE_SENTINEL_METRIC_IDS
_EXPORT_SOURCE_LABEL = "National Council on Teacher Quality (NCTQ) Compass"
_PUBLIC_CSV_COLUMNS = [
    "Export Source",
    "District",
    "State",
    "Measure",
    "Value",
    "School Year",
    "Data Status",
    "Data Note",
    "Citation Marker(s)",
    "Source Document",
    "Source Type",
    "Source Page(s)",
    "Source URL(s)",
]
_PUBLIC_SORT_CSV_COLUMNS = [
    "Ranked By",
    "Rank Value",
    "Rank School Year",
    "Rank Source URL",
]


# ─── CSV export helpers (typed per variant family) ───────────────────────────


def lookup_renders_stale_prior_values(result: "ResultSet") -> bool:
    """#1826 — does this result promote stale rows into the rendered table?

    Only the **single-metric** ``metric_lookup`` surface keeps stale rows (their
    prior-year value) in the table instead of demoting them to prose (#1514). It
    is the one surface where a current-value-less row is coherent in a table: a
    lookup is an unordered value display, so a prior-year value can occupy a row
    with its year annotated. Ranking orders *by value* (a stale row has no rank),
    trend is time-ordered, peer is value-based, count aggregates buckets, and the
    multi-metric lookup pivot has no per-cell year column to annotate staleness —
    so all of those keep the #1514 prose-demote.

    Result-only (no plan) so every surface that must agree — the table, the CSV
    data rows, the manifest ``displayed_row_count``, and the surface_consistency
    parity validators — keys the *same* extended row subset. (The state-grouped
    lookup is also single-metric; its table aggregates covered rows only and its
    stale rows stay narrated, handled explicitly at the render call sites, so the
    per-district CSV/parity subset can safely include them without divergence.)
    """

    if not isinstance(result, MetricLookupResult):
        return False
    metric_ids = {row.metric_id for row in result.rows}
    return len(metric_ids) <= 1


def _renders_in_answer_table(row: object, *, include_stale: bool) -> bool:
    """Row-level render-inclusion gate shared by the CSV builders (#1826).

    An answer row (value / INA / N/A, or a ``None`` pre-label) always renders;
    when ``include_stale`` (single-metric lookup), a stale prior-value row joins
    them. Mirrors ``rendering/writer._answer_rows`` so the CSV data rows track
    the visible table exactly.
    """

    if is_rendered_answer_state(getattr(row, "coverage_state", None)):
        return True
    return include_stale and is_stale_prior_value_row(row)


def _default_csv_export_for_result(
    result: (
        MetricRankingResult
        | MetricLookupResult
        | ProfileLookupResult
        | PeerComparisonResult
    ),
) -> CsvExportArtifact:
    """CSV export shared by ranking, lookup, profile, and peer variants.

    All four variants share `_CSV_COLUMNS` and the same row-projection shape.
    The four row types differ in which sort/criterion/coverage fields exist;
    `getattr(row, …, None)` covers the cross-variant differences without
    losing the per-variant `rows` narrowing on the input.

    #1514 D2 — the CSV mirrors the table: data rows are answer rows only
    (`is_rendered_answer_state`, the shared None-as-answer row predicate);
    not_reviewed and out_of_universe rows are voiced as narrative sentences
    in the rendered answer, never data points. The full record stays on
    `result.rows`.
    """

    citation_by_marker = {citation.marker: citation for citation in result.citations}
    # #1826: the single-metric lookup keeps stale rows (their prior-year value)
    # in the table, so its CSV data rows include them too — the raw record (the
    # current academic_year plus the populated coverage_prior_* columns), never a
    # display swap. Every other surface keeps the #1514 answers-only filter.
    include_stale = lookup_renders_stale_prior_values(result)
    rows = []
    for row in result.rows:
        if not _renders_in_answer_table(row, include_stale=include_stale):
            continue
        source_metadata = _source_metadata_for_markers(
            row.citation_markers,
            citation_by_marker,
        )
        sort_source_metadata = _sort_source_metadata_for_row(row)
        rows.append(
            {
                "district_id": row.district_id,
                "district_name": row.district_name,
                "state": row.state,
                "metric_id": row.metric_id,
                "metric_name": row.metric_name,
                "value": row.value,
                "display_value": row.display_value,
                "sort_metric_id": getattr(row, "sort_metric_id", None),
                "sort_metric_name": getattr(row, "sort_metric_name", None),
                "sort_value": getattr(row, "sort_value", None),
                "sort_display_value": getattr(row, "sort_display_value", None),
                "sort_academic_year": getattr(row, "sort_academic_year", None),
                **sort_source_metadata,
                "criterion_id": getattr(row, "criterion_id", None),
                "criterion_label": getattr(row, "criterion_label", None),
                "criterion_satisfied": getattr(row, "criterion_satisfied", None),
                "academic_year": row.academic_year,
                "coverage_state": row.coverage_state,
                "coverage_display": row.coverage_display,
                "coverage_reason": row.coverage_reason,
                "coverage_qualifier": getattr(row, "coverage_qualifier", None),
                "coverage_prior_academic_year": getattr(
                    row, "coverage_prior_academic_year", None
                ),
                "coverage_prior_display_value": getattr(
                    row, "coverage_prior_display_value", None
                ),
                "citation_markers": " ".join(str(marker) for marker in row.citation_markers),
                **source_metadata,
            }
        )
    disclosures = list(result.coverage_disclosures)
    if isinstance(result, MetricRankingResult):
        # #1514 D2 — ranking CSVs keep today's behavior: every non-stale
        # district without a current answer still appears exactly once,
        # appended after the data rows. Execution removed promoted districts
        # from coverage_disclosures (they live in result.rows); re-derive
        # them here so the answers-only data filter above doesn't erase
        # them from the download.
        disclosures = [*promoted_row_disclosures(result), *disclosures]
    rows.extend(_coverage_disclosure_csv_rows(disclosures))
    public_columns, public_rows = _public_csv_export(rows)
    return CsvExportArtifact(
        columns=list(_CSV_COLUMNS),
        rows=rows,
        public_columns=public_columns,
        public_rows=public_rows,
    )


def _trend_csv_export_for_result(result: "MetricTrendResult") -> CsvExportArtifact:
    citation_by_marker = {citation.marker: citation for citation in result.citations}
    rows = []
    for row in result.rows:
        # #1514 D2 — answers-only data rows; see _default_csv_export_for_result.
        if not is_rendered_answer_state(row.coverage_state):
            continue
        source_metadata = _source_metadata_for_markers(
            row.citation_markers,
            citation_by_marker,
        )
        rows.append(
            {
                "district_id": row.district_id,
                "district_name": row.district_name,
                "state": row.state,
                "metric_id": row.metric_id,
                "metric_name": row.metric_name,
                "value": row.value,
                "display_value": row.display_value,
                "numeric_value": row.numeric_value,
                "academic_year": row.academic_year,
                "coverage_state": row.coverage_state,
                "coverage_display": row.coverage_display,
                "coverage_reason": row.coverage_reason,
                "coverage_qualifier": row.coverage_qualifier,
                "coverage_prior_academic_year": row.coverage_prior_academic_year,
                "coverage_prior_display_value": row.coverage_prior_display_value,
                "delta_value": row.delta_value,
                "delta_percent": row.delta_percent,
                "delta_from_academic_year": row.delta_from_academic_year,
                "citation_markers": " ".join(str(marker) for marker in row.citation_markers),
                **source_metadata,
            }
        )
    public_columns, public_rows = _public_csv_export(rows)
    return CsvExportArtifact(
        columns=list(_TREND_CSV_COLUMNS),
        rows=rows,
        public_columns=public_columns,
        public_rows=public_rows,
    )


def _count_csv_export_for_result(result: "MetricCountResult") -> CsvExportArtifact:
    citation_by_marker = {citation.marker: citation for citation in result.citations}
    rows = []
    for row in result.rows:
        # #1514 D12 — the count CSV mirrors the table and chart: answer
        # buckets only. "Not reviewed"/"Unavailable" categorical buckets stay
        # on result.rows (the full record) and are voiced as the rule-2
        # narrative sentence — they leave the CSV exactly like they left the
        # table and chart. Same shared row predicate as every other surface.
        if not is_rendered_answer_state(row.coverage_state):
            continue
        source_metadata = _source_metadata_for_markers(
            row.citation_markers,
            citation_by_marker,
        )
        rows.append(
            {
                "metric_id": row.metric_id,
                "metric_name": row.metric_name,
                "count_kind": row.count_kind,
                "category": row.category if isinstance(row, CategoricalCountRow) else "",
                "count": row.count,
                "denominator": row.denominator,
                "percent": "" if row.percent is None else row.percent,
                "filter_statement": row.filter_statement,
                "qualifying_district_ids": " ".join(
                    str(district_id) for district_id in row.qualifying_district_ids
                ),
                "source": row.source,
                "display_value": row.display_value,
                "academic_year": row.academic_year,
                "value": row.value,
                "coverage_state": row.coverage_state,
                "coverage_display": row.coverage_display,
                "coverage_reason": row.coverage_reason,
                "coverage_qualifier": None,
                "coverage_prior_academic_year": None,
                "coverage_prior_display_value": None,
                "citation_markers": " ".join(
                    str(marker) for marker in row.citation_markers
                ),
                **source_metadata,
            }
        )
    return CsvExportArtifact(columns=list(_COUNT_CSV_COLUMNS), rows=rows)


def promoted_row_disclosures(result: ResultSet) -> list[CoverageDisclosure]:
    """Disclosures re-derived from non-answer rows held in ``result.rows``.

    Execution promotes explicit-scope districts without a current answer into
    ``result.rows`` and removes them from ``coverage_disclosures`` so no
    district is represented twice. #1514 moves those rows out of the rendered
    table and the CSV's data rows, so both the composer (narrative sentences)
    and the ranking CSV builder (appended disclosure rows) re-derive them
    through this one helper — at presentation time only; the ResultSet stays
    the full record (V-10 guard). Deduplicated against the disclosures the
    result already carries.
    """

    seen = {
        (disclosure.district_id, disclosure.metric_id)
        for disclosure in result.coverage_disclosures
    }
    disclosures: list[CoverageDisclosure] = []
    for row in result.rows:
        state = getattr(row, "coverage_state", None)
        if is_rendered_answer_state(state):
            continue
        key = (row.district_id, row.metric_id)
        if key in seen:
            continue
        seen.add(key)
        disclosures.append(
            CoverageDisclosure(
                district_id=row.district_id,
                district_name=row.district_name,
                state=row.state,
                metric_id=row.metric_id,
                metric_name=row.metric_name,
                academic_year=row.academic_year,
                coverage_state=state,
                display=getattr(row, "coverage_display", None) or row.display_value,
                reason=getattr(row, "coverage_reason", None),
                qualifier=getattr(row, "coverage_qualifier", None),
                prior_academic_year=getattr(
                    row, "coverage_prior_academic_year", None
                ),
                prior_display_value=getattr(
                    row, "coverage_prior_display_value", None
                ),
            )
        )
    return disclosures


# Rank columns that duplicate a base column when a query ranks by the metric it
# displays. Each pair drops the rank column when it equals its base in every row
# (#1611 rule 2); a genuine cross-metric sort keeps them because the values differ.
_PUBLIC_RANK_BASE_PAIRS = [
    ("Ranked By", "Measure"),
    ("Rank Value", "Value"),
    ("Rank School Year", "School Year"),
    ("Rank Source URL", "Source URL(s)"),
]


def _public_csv_export(
    rows: list[dict[str, object]],
) -> tuple[list[str], list[dict[str, object]]]:
    # Empty-rows guard: never emit a zero-column CSV. With no rows to prove
    # which columns carry signal, return the base schema unpruned (#1611).
    if not rows:
        return list(_PUBLIC_CSV_COLUMNS), []
    # Build the full candidate (every public column + every rank column,
    # projected for each row), then prune. The two prune rules read the public
    # projected values, so rank↔base equality compares the same strings the
    # user sees.
    columns = [*_PUBLIC_CSV_COLUMNS, *_PUBLIC_SORT_CSV_COLUMNS]
    public_rows = [_public_csv_row(row) for row in rows]
    return _prune_public_columns(columns, public_rows)


def _prune_public_columns(
    columns: list[str],
    rows: list[dict[str, object]],
) -> tuple[list[str], list[dict[str, object]]]:
    """Drop noise columns from the built public projection (#1611).

    Two general, per-column rules over the public projected values:

    1. **Empty columns** — a column whose value is empty (``""``/``None``) in
       every row is dropped. No hardcoded names (drops e.g. ``Data Note`` and
       an unused ``Rank Source URL``); a varying column — e.g. ``Data Note``
       non-empty on a "not reviewed" disclosure row — is kept.
    2. **Redundant rank columns** — a rank column equal to its base column in
       every row is dropped (ranked by the displayed metric). A genuine
       cross-metric sort, where rank values differ from displayed values,
       keeps them.

    Order is immaterial; both conditions are "true across all rows". Pure: it
    takes the built columns + projected rows and returns the pruned pair.
    """

    def _empty(value: object) -> bool:
        return value is None or value == ""

    drop: set[str] = set()
    for column in columns:
        if all(_empty(row.get(column)) for row in rows):
            drop.add(column)
    for rank_column, base_column in _PUBLIC_RANK_BASE_PAIRS:
        if rank_column in drop:
            continue
        if all(row.get(rank_column) == row.get(base_column) for row in rows):
            drop.add(rank_column)
    kept_columns = [column for column in columns if column not in drop]
    pruned_rows = [
        {column: row[column] for column in kept_columns} for row in rows
    ]
    return kept_columns, pruned_rows


def _public_csv_row(
    row: dict[str, object],
) -> dict[str, object]:
    status, note = _public_data_status(row)
    public_row: dict[str, object] = {
        "Export Source": _EXPORT_SOURCE_LABEL,
        "District": row.get("district_name") or "",
        "State": row.get("state") or "",
        "Measure": row.get("metric_name") or "",
        "Value": row.get("display_value") or row.get("value") or "",
        "School Year": row.get("academic_year") or "",
        "Data Status": status,
        "Data Note": note,
        "Citation Marker(s)": row.get("citation_markers") or "",
        "Source Document": row.get("source_document") or "",
        "Source Type": row.get("source_document_type") or "",
        "Source Page(s)": row.get("source_page") or "",
        "Source URL(s)": row.get("source_urls") or row.get("source_url") or "",
        # Rank columns are always projected; _prune_public_columns drops them
        # when they're empty (rule 1) or duplicate their base column (rule 2).
        "Ranked By": row.get("sort_metric_name") or "",
        "Rank Value": row.get("sort_display_value") or row.get("sort_value") or "",
        "Rank School Year": row.get("sort_academic_year") or "",
        "Rank Source URL": row.get("sort_source_url") or "",
    }
    return public_row


def _public_data_status(row: dict[str, object]) -> tuple[str, str]:
    coverage_state = row.get("coverage_state")
    coverage_reason = row.get("coverage_reason")
    coverage_display = row.get("coverage_display")
    fallback = str(coverage_display or row.get("display_value") or "")
    if coverage_state == "covered":
        return "Available", ""
    status = short_coverage_label(
        str(coverage_reason) if coverage_reason is not None else None,
        fallback,
    )
    note = fallback if fallback and fallback != status else ""
    return status, note


def _coverage_disclosure_csv_rows(
    disclosures: list[CoverageDisclosure],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for disclosure in disclosures:
        # Stale prior-year values are voiced as the canonical narrative
        # sentence ("NCTQ last reviewed {district} … ; the value then was …")
        # in the rendered answer (#1514); re-enumerating them inline in the
        # CSV next to the ranked set invites misreading them as current, so
        # the CSV keeps dropping them (#1435).
        if disclosure.reason == "stale_recent_answer":
            continue
        rows.append(
            {
                "district_id": disclosure.district_id,
                "district_name": disclosure.district_name,
                "state": disclosure.state,
                "metric_id": disclosure.metric_id,
                "metric_name": disclosure.metric_name,
                "value": None,
                "display_value": disclosure.display,
                "sort_metric_id": None,
                "sort_metric_name": None,
                "sort_value": None,
                "sort_display_value": None,
                "sort_academic_year": None,
                "sort_source_document": "",
                "sort_source_document_type": "",
                "sort_source_url": "",
                "sort_source_valid_from": "",
                "sort_source_valid_to": "",
                "criterion_id": None,
                "criterion_label": None,
                "criterion_satisfied": None,
                "academic_year": disclosure.academic_year,
                "coverage_state": disclosure.coverage_state,
                "coverage_display": disclosure.display,
                "coverage_reason": disclosure.reason,
                "coverage_qualifier": disclosure.qualifier,
                "coverage_prior_academic_year": disclosure.prior_academic_year,
                "coverage_prior_display_value": disclosure.prior_display_value,
                "citation_markers": "",
                "source_document": "",
                "source_document_type": "",
                "source_page": "",
                "source_url": "",
                "source_valid_from": "",
                "source_valid_to": "",
                "source_urls": "",
            }
        )
    return rows


def _source_metadata_for_markers(
    markers: list[int],
    citation_by_marker: dict[int, CitationRef],
) -> dict[str, str]:
    row_citations = [
        citation_by_marker[marker]
        for marker in markers
        if marker in citation_by_marker
    ]
    source_urls = [citation.url for citation in row_citations if citation.url]
    valid_from, valid_to = _citation_valid_years(row_citations)
    return {
        "source_document": " | ".join(citation.title for citation in row_citations),
        "source_document_type": " | ".join(
            citation.document_type or "" for citation in row_citations
        ),
        "source_page": " | ".join(
            citation.page_ref
            or (f"p. {citation.page_number}" if citation.page_number else "")
            for citation in row_citations
            if citation.page_ref or citation.page_number
        ),
        "source_url": " | ".join(source_urls),
        "source_valid_from": valid_from,
        "source_valid_to": valid_to,
        "source_urls": " ".join(source_urls),
    }


def _sort_source_metadata_for_row(row: object) -> dict[str, str]:
    explicit_document = getattr(row, "sort_source_document", None)
    explicit_type = getattr(row, "sort_source_document_type", None)
    explicit_url = getattr(row, "sort_source_url", None)
    explicit_valid_from = getattr(row, "sort_source_valid_from", None)
    explicit_valid_to = getattr(row, "sort_source_valid_to", None)
    if any(
        (
            explicit_document,
            explicit_type,
            explicit_url,
            explicit_valid_from,
            explicit_valid_to,
        )
    ):
        return {
            "sort_source_document": explicit_document or "",
            "sort_source_document_type": explicit_type or "",
            "sort_source_url": explicit_url or "",
            "sort_source_valid_from": explicit_valid_from or "",
            "sort_source_valid_to": explicit_valid_to or "",
        }

    if getattr(row, "sort_metric_id", None) in _PROFILE_SORT_METRIC_IDS:
        return {
            "sort_source_document": "NCES district profile",
            "sort_source_document_type": "district_profile",
            "sort_source_url": "",
            "sort_source_valid_from": _first_year(getattr(row, "sort_academic_year", None)),
            "sort_source_valid_to": "",
        }
    return {
        "sort_source_document": "",
        "sort_source_document_type": "",
        "sort_source_url": "",
        "sort_source_valid_from": "",
        "sort_source_valid_to": "",
    }


def _first_year(value: str | None) -> str:
    if not value:
        return ""
    for token in value.replace("-", " ").split():
        if len(token) == 4 and token.isdigit():
            return token
    return ""


def _should_render_chart(values: list[float]) -> bool:
    """Determine whether a chart should be rendered based on sample size and variance.

    Returns False when there are fewer than 3 numeric points OR when all points
    round to the same value (at ≥6 decimal places precision). Otherwise returns True.

    Args:
        values: Parsed numeric values to check.

    Returns:
        True if the chart should be rendered, False if it should be suppressed.
    """
    if len(values) < 3:
        return False
    distinct = len({round(v, 6) for v in values})
    if distinct < 2:
        return False
    return True


def _chart_for_ranking(result: "MetricRankingResult") -> ChartArtifact | None:
    rows = [row for row in result.rows if row.coverage_state == "covered"]
    if not rows:
        return None
    metric_name = rows[0].metric_name
    points = []
    numeric_values = []
    for row in rows:
        numeric_value = _parse_chart_numeric_value(row.value)
        if numeric_value is None:
            continue
        numeric_values.append(numeric_value)
        points.append(
            ChartPoint(
                label=row.district_name,
                value=numeric_value,
                district_id=row.district_id,
                citation_markers=row.citation_markers,
            )
        )
    if not points:
        return None
    if not _should_render_chart(numeric_values):
        return None
    return ChartArtifact(
        title=f"{metric_name} by district",
        x_axis_label="District",
        y_axis_label=metric_name,
        points=points,
    )


def _chart_for_metric_lookup(
    result: "MetricLookupResult | PeerComparisonResult",
) -> ChartArtifact | None:
    metric_rows: dict[int, list[MetricValueRow | PeerComparisonRow]] = {}
    metric_names: dict[int, str] = {}
    for row in result.rows:
        if row.coverage_state != "covered":
            continue
        numeric_value = _parse_chart_numeric_value(row.value)
        if numeric_value is None:
            continue
        metric_rows.setdefault(row.metric_id, []).append(row)
        metric_names[row.metric_id] = row.metric_name

    if not metric_rows:
        return None

    series = []
    for metric_id, rows in sorted(
        metric_rows.items(),
        key=lambda item: metric_names[item[0]].casefold(),
    ):
        points = [
            ChartPoint(
                label=row.district_name,
                value=_parse_chart_numeric_value(row.value) or 0.0,
                district_id=row.district_id,
                citation_markers=row.citation_markers,
            )
            for row in rows
        ]
        numeric_values = [p.value for p in points]
        if _should_render_chart(numeric_values):
            series.append(
                ChartSeries(
                    label=metric_names[metric_id],
                    metric_id=metric_id,
                    points=points,
                )
            )

    if not series:
        return None

    return ChartArtifact(
        title="Selected metrics by district",
        x_axis_label="District",
        y_axis_label="Value",
        points=series[0].points if len(series) == 1 else [],
        series=series,
        show_legend=len(series) > 1,
    )


def _chart_for_metric_count(result: "MetricCountResult") -> ChartArtifact | None:
    # #1514 D12 — the chart mirrors the table: answer buckets only. The
    # "Not reviewed"/"Unavailable" bucket rows stay in result.rows (full
    # record) and become the rule-2 narrative sentence, never chart bars.
    # `is_rendered_answer_state` is the shared None-as-answer row predicate,
    # so table, CSV, and chart agree on pre-labeling rows.
    rows = [
        row
        for row in result.rows
        if isinstance(row, CategoricalCountRow)
        and is_rendered_answer_state(row.coverage_state)
    ]
    if not rows:
        return None
    metric_name = rows[0].metric_name
    points = [
        ChartPoint(
            label=row.category,
            value=float(row.count),
            citation_markers=row.citation_markers,
        )
        for row in rows
    ]
    numeric_values = [p.value for p in points]
    if not _should_render_chart(numeric_values):
        return None
    return ChartArtifact(
        title=f"{metric_name} distribution",
        x_axis_label="Category",
        y_axis_label="District count",
        points=points,
    )


def _parse_chart_numeric_value(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    cleaned = str(value).strip().replace("$", "").replace(",", "").replace("%", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _citation_valid_years(citations: list[CitationRef]) -> tuple[str, str]:
    for citation in citations:
        years = _split_academic_year(citation.academic_year)
        if years is not None:
            return years
    return "", ""


def _split_academic_year(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split("-")]
    if len(parts) != 2:
        return None
    if not all(part.isdigit() for part in parts):
        return None
    return parts[0], parts[1]
