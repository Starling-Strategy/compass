"""Deterministic metric-ranking operation path."""

from __future__ import annotations

import re

from compass_backend.artifacts import (
    CitationRef,
    CoverageDisclosure,
    CoverageLabel,
    LargestExcludedDisclosure,
    LargestExcludedDistrict,
    MethodologyRef,
    REVIEWED_NO_FIGURE_STATES,
    MetricRankingResult,
    RankingRow,
    ResultSelection,
    SelectedDistrict,
    coverage_frame_from_labels,
    coverage_label_for_answer,
    coverage_label_for_missing_answer,
    coverage_label_for_stale_answer,
)
from compass_backend.artifacts.currency import (
    metric_name_is_currency,
    whole_dollar_display,
)
from compass_backend.catalog import MetricCandidate, ProfileFieldRankRow
from compass_backend.catalog.nces_allowlist import nces_allowlist_by_field_key
from compass_backend.contracts.planning import QueryPlan

from ._text_utils import (
    _BOOLEAN_FALSE_VALUES,
    _BOOLEAN_TRUE_VALUES,
)
from .evidence import citation_markers_for_row
from .selection import (
    direction,
    direction_phrase,
    requested_states,
    select_rows,
    state_filters_are_supported,
)
from .types import MetricAnswerRow

_PROFILE_FIELD_METRIC_IDS = {
    "enrollment": -1001,
    "frpl_pct": -1002,
}

# Tie-at-the-cutoff expansion cap (#1471). When the value at the requested
# cutoff is shared with the next district(s) below it, every district tied at
# that cutoff value is included so the table never silently drops a tied
# district — 05-accuracy-sort.md commits to this. To keep a pathological tie
# (dozens of districts sharing one value) from blowing a "top 5" into a 40-row
# table, expansion stops once the total would exceed ``limit + _MAX_TIE_EXPANSION``
# and the count of tied districts not shown is disclosed. The small-tie case (a
# handful of extra rows) always expands; only a large tie is capped + disclosed.
_MAX_TIE_EXPANSION = 5

def _parse_rankable_metric_value(row: MetricAnswerRow) -> float | None:
    """Numeric sort key for a row, with a boolean-aware fallback.

    Parse-once: the numeric case reads the typed value the row already carries
    from its boundary (``MetricAnswerRow.numeric_value``) rather than re-parsing
    ``row.value``. The boolean fallback ("Yes"/"No" -> 1.0/0.0) is a
    ranking-only rule that the canonical numeric parser does not (and should not)
    encode, so it stays here and reads the raw ``row.value``.
    """

    if row.numeric_value is not None:
        return row.numeric_value
    if row.value is None:
        return None
    normalized = str(row.value).casefold().replace(",", "").strip().strip(".")
    if normalized in _BOOLEAN_TRUE_VALUES:
        return 1.0
    if normalized in _BOOLEAN_FALSE_VALUES:
        return 0.0
    return None


def ranking_shape_is_supported(plan: QueryPlan) -> bool:
    """Return whether the ranking path supports this query plan."""

    metrics = _ranking_result_metrics(plan)
    return (
        plan.operation == "rank"
        and plan.selection.scope
        in {"all_covered_districts", "state", "named_districts", "largest_districts"}
        and len(metrics) == 1
        and metrics[0].role == "primary"
        and state_filters_are_supported(plan)
    )


def build_metric_ranking_result(
    plan: QueryPlan,
    metric: MetricCandidate,
    rows: list[MetricAnswerRow],
    recent_rows: list[MetricAnswerRow],
    *,
    selection: ResultSelection,
    selected_districts: list[SelectedDistrict],
    default_limit: int,
    reviewed_district_ids: set[int],
    academic_year: str,
    source_notes: list[str] | None = None,
    shared_citation_refs: list[CitationRef] | None = None,
    shared_citation_marker_by_identity: dict[tuple[object, ...], int] | None = None,
) -> MetricRankingResult:
    """Build a ranked metric result from resolved catalog IDs and answer rows.

    ``shared_citation_refs`` / ``shared_citation_marker_by_identity`` thread
    a single citation-dedup state across multiple sibling children when a
    composite-ranking envelope is being built (W2-M5-04 / #748). When both
    are passed (the composite case), every sibling child resulting from
    repeated calls shares the same citation list reference and identity
    map — so the same document URL cited from child A and child B
    consolidates into one ``CitationRef`` and one inline marker number.
    Pydantic's ``frozen=True`` does not deep-copy mutable container fields,
    so when later children mutate the shared list the already-frozen child
    artifacts continue to point at the same (now-fuller) list. When the
    kwargs are ``None`` (the standalone case), fresh per-call structures
    keep the existing single-table behaviour byte-identical.
    """

    sort_direction = direction(plan)
    reverse = sort_direction == "desc"
    selected_rows = select_rows(plan, rows, selection)
    selected_recent_rows = select_rows(plan, recent_rows, selection)
    selected_cells = _selected_ranking_cells(
        rows=selected_rows,
        recent_rows=selected_recent_rows,
        selected_districts=selected_districts,
        metric=metric,
        academic_year=academic_year,
    )
    candidates: list[tuple[MetricAnswerRow, float]] = []
    coverage_by_district: dict[int, CoverageLabel] = {}
    coverage_disclosures: list[CoverageDisclosure] = []
    # Track districts with no current AND no recent row — they are the
    # "truly missing" districts that should be appended below the ranked
    # block rather than silently dropped.
    truly_missing_district_ids: set[int] = set()

    for district, row, recent_row in selected_cells:
        if row is None and recent_row is not None:
            coverage = coverage_label_for_stale_answer(
                recent_row.value,
                district_name=district.district_name,
                metric_name=metric.name,
                current_academic_year=academic_year,
                prior_academic_year=recent_row.academic_year,
                metric_topic=_metric_topic(metric),
            )
            coverage_by_district[district.district_id] = coverage
            coverage_disclosures.append(
                _coverage_disclosure(
                    district,
                    metric,
                    academic_year,
                    coverage,
                    prior_academic_year=recent_row.academic_year,
                    prior_display_value=str(recent_row.value),
                )
            )
            continue
        if row is None:
            coverage = coverage_label_for_missing_answer(
                district_name=district.district_name,
                metric_name=metric.name,
                academic_year=academic_year,
                district_has_current_year_rows=(
                    district.district_id in reviewed_district_ids
                ),
                metric_topic=_metric_topic(metric),
            )
            coverage_by_district[district.district_id] = coverage
            coverage_disclosures.append(
                _coverage_disclosure(district, metric, academic_year, coverage)
            )
            truly_missing_district_ids.add(district.district_id)
            continue
        coverage = coverage_label_for_answer(
            row.value,
            district_name=row.district_name,
            metric_name=metric.name,
            academic_year=row.academic_year,
        )
        coverage_by_district[district.district_id] = coverage
        if coverage.state != "covered":
            coverage_disclosures.append(
                _coverage_disclosure(district, metric, row.academic_year, coverage)
            )
            continue
        numeric_value = _parse_rankable_metric_value(row)
        if numeric_value is None:
            coverage = coverage.model_copy(
                update={"reason": "non_numeric_rank_exclusion"}
            )
            coverage_by_district[district.district_id] = coverage
            coverage_disclosures.append(
                _coverage_disclosure(district, metric, row.academic_year, coverage)
            )
            continue
        candidates.append((row, numeric_value))

    candidates.sort(
        key=lambda item: (
            -item[1] if reverse else item[1],
            item[0].district_name.casefold(),
        )
    )
    limit = _limit(plan)
    limited, tie_note = _expand_ties_at_cutoff(candidates, limit)
    order = "highest to lowest" if reverse else "lowest to highest"
    citation_refs: list[CitationRef] = (
        shared_citation_refs if shared_citation_refs is not None else []
    )
    citation_marker_by_identity: dict[tuple[object, ...], int] = (
        shared_citation_marker_by_identity
        if shared_citation_marker_by_identity is not None
        else {}
    )
    result_rows: list[RankingRow] = []

    # Populate sort_* mirrors on every ranked row so downstream validators
    # and multi-turn follow-ups that reference the sort field by name (e.g.,
    # "for those N districts, sorted by <metric>") see the numeric sort key
    # directly on the row, symmetric with the profile-ordered ranking path.
    is_currency = metric_name_is_currency(metric.name)
    for index, (row, numeric_value) in enumerate(limited, start=1):
        coverage = coverage_by_district[row.district_id]
        # #1687: normalize currency columns to whole dollars at value-formation
        # so the table cell, the (ranked) sort key, and the CSV export all read
        # the same precision — never "$71,038.98" beside "$71,038". Non-currency
        # values and unparseable amounts keep their raw display unchanged. Update
        # the coverage label's display in lockstep so display_value ==
        # coverage_display (the coverage_display_mismatch guard) — mirrors
        # execution/peer.py's whole-dollar normalization.
        row_display = str(row.value)
        if is_currency and coverage.state == "covered":
            whole_dollar = whole_dollar_display(row.value)
            if whole_dollar is not None:
                row_display = whole_dollar
                coverage = coverage.model_copy(update={"display": whole_dollar})
        result_rows.append(
            RankingRow(
                district_id=row.district_id,
                district_name=row.district_name,
                state=row.state,
                metric_id=metric.metric_id,
                metric_name=metric.name,
                value=numeric_value,
                display_value=row_display,
                academic_year=row.academic_year,
                rank=index,
                citation_markers=citation_markers_for_row(
                    row,
                    citation_refs,
                    citation_marker_by_identity,
                ),
                coverage_state=coverage.state,
                coverage_display=coverage.display,
                coverage_reason=coverage.reason,
                coverage_qualifier=coverage.qualifier,
                sort_metric_id=metric.metric_id,
                sort_metric_name=metric.name,
                sort_value=numeric_value,
                sort_display_value=row_display,
                sort_academic_year=row.academic_year,
            )
        )

    # For user-named scopes (named_districts, state), the user named these
    # districts — a district with no current row should be visible in the table
    # with a coverage label, not silently dropped.
    #
    # For ``largest_districts`` the rule is different (#1468): "the N largest by
    # [metric]" means the N largest that are genuinely *comparable* — i.e. that
    # carry a rankable value. A larger district lacking that value must NOT pad
    # the comparable table; it is disclosed separately via ``largest_excluded``
    # below. So ``largest_districts`` is intentionally excluded from the
    # missing-row padding here.
    #
    # For the open-universe scope (all_covered_districts), absent metric rows
    # mean the district simply hasn't been reviewed yet; those districts are
    # correctly represented in coverage_disclosures only.
    pad_missing_into_table = selection.scope in {"named_districts", "state"}
    missing_cells = (
        [
            (district, coverage_by_district[district.district_id])
            for district in selected_districts
            if district.district_id in truly_missing_district_ids
        ]
        if pad_missing_into_table
        else []
    )
    # Sort missing rows alphabetically so output order is deterministic.
    missing_cells.sort(key=lambda item: item[0].district_name.casefold())

    # Append missing-data rows up to the limit (fill remaining slots).
    # If limit is None, append all; if limit is set, fill to limit - len(limited).
    slots_available = (
        None if limit is None else max(0, limit - len(limited))
    )
    missing_to_include = (
        missing_cells if slots_available is None else missing_cells[:slots_available]
    )
    # IDs of truly-missing districts that ARE promoted into result_rows.
    # These must be removed from coverage_disclosures so the CSV exporter
    # doesn't double-emit them (once as a result row, once as a disclosure row).
    # Districts truncated by the limit that did NOT make it into result_rows
    # remain in coverage_disclosures so the user still has information about
    # them.
    promoted_district_ids = {district.district_id for district, _ in missing_to_include}
    coverage_disclosures = [
        d for d in coverage_disclosures if d.district_id not in promoted_district_ids
    ]

    for offset, (district, coverage) in enumerate(missing_to_include, start=1):
        result_rows.append(
            RankingRow(
                district_id=district.district_id,
                district_name=district.district_name,
                state=district.state,
                metric_id=metric.metric_id,
                metric_name=metric.name,
                value=None,
                display_value=coverage.display,
                academic_year=academic_year,
                rank=len(limited) + offset,
                source="coverage_state",
                citation_markers=[],
                coverage_state=coverage.state,
                coverage_display=coverage.display,
                coverage_reason=coverage.reason,
                coverage_qualifier=coverage.qualifier,
            )
        )

    coverage_labels = list(coverage_by_district.values())
    # excluded_count: districts that could not participate in rank ordering
    # (missing, stale, non-numeric).  Districts that were ranked but fall below
    # the limit cut, and missing-data districts appended below the ranked block,
    # are intentionally not included here — they are either shown in the table
    # or accounted for via coverage_disclosures.
    excluded_count = len(coverage_labels) - len(candidates)

    # #1468: for "the N largest by [metric]", name the larger districts left out
    # because they carry no comparable value, split into the honest INA buckets.
    ranked_district_ids = {row.district_id for row, _ in limited}
    largest_excluded = _largest_excluded_disclosure(
        selection=selection,
        selected_districts=selected_districts,
        coverage_by_district=coverage_by_district,
        ranked_district_ids=ranked_district_ids,
    )

    return MetricRankingResult(
        selection=selection,
        rows=result_rows,
        citations=citation_refs,
        coverage_frame=coverage_frame_from_labels(coverage_labels),
        coverage_disclosures=coverage_disclosures,
        total_considered=len(coverage_labels),
        excluded_count=excluded_count,
        largest_excluded=largest_excluded,
        order_statement=f"Ranked{_scope_phrase(plan)} by {metric.name}, {order}.",
        source_notes=[*(source_notes or []), *([tie_note] if tie_note else [])],
        methodology_codes=[
            MethodologyRef(code="citation_answer_level_preferred_source_fallback"),
        ],
    )


def build_profile_ranking_result(
    plan: QueryPlan,
    rows: list[ProfileFieldRankRow],
    *,
    selection: ResultSelection,
    direction: str,
) -> MetricRankingResult:
    """Build a ranked result from deterministic profile-field rows.

    Per W2-M5-03 (#747): every profile-field-ranked row carries an NCES
    citation pulled from the governed allowlist (migration 060 / W2-M5-00a).
    The Ashley Golden29 sheet rows 7/8/9 reported empty Sources columns
    on three back-to-back enrollment-rank queries; this builder now reads
    the canonical_url + canonical_title for the row's ``field_key`` and
    emits one CitationRef shared across all rows (W2-M5-04 URL-dedup
    pattern — same NCES URL → one Sources entry, one inline marker).
    """

    order = direction_phrase(direction)
    label = rows[0].label if rows else "profile field"
    coverage_labels = [
        coverage_label_for_answer(
            row.display_value,
            district_name=row.district_name,
            metric_name=row.label,
            academic_year=row.academic_year,
        ).model_copy(update={"reason": "profile_field_value"})
        for row in rows
    ]

    allowlist = nces_allowlist_by_field_key()
    citations: list[CitationRef] = []
    marker_by_field_key: dict[str, int] = {}
    for row in rows:
        entry = allowlist.get(row.field_key)
        if entry is None or row.field_key in marker_by_field_key:
            continue
        marker = len(citations) + 1
        marker_by_field_key[row.field_key] = marker
        citations.append(
            CitationRef(
                marker=marker,
                title=entry.canonical_title,
                url=entry.canonical_url,
                academic_year=row.academic_year,
                document_type="NCES profile field",
            )
        )

    result_rows = [
        RankingRow(
            district_id=row.district_id,
            district_name=row.district_name,
            state=row.state,
            metric_id=_PROFILE_FIELD_METRIC_IDS.get(row.field_key, -1999),
            metric_name=row.label,
            # ProfileFieldRankRow.value is already a strict float (validated at
            # the catalog boundary), so the old float() wrapper was a no-op. Drop
            # it: passing the typed value straight through keeps the single
            # string->number authority the only place a string is coerced, and
            # leaves zero bare value-coercion sites for the §8b guard to flag.
            value=row.value,
            display_value=row.display_value,
            academic_year=row.academic_year,
            rank=index,
            source="profile_field",
            citation_markers=(
                [marker_by_field_key[row.field_key]]
                if row.field_key in marker_by_field_key
                else []
            ),
            coverage_state="covered",
            coverage_display=row.display_value,
            coverage_reason="profile_field_value",
            coverage_qualifier=row.vintage,
            # W0-01: every ranking row must carry the sort-proof contract so
            # quality/validators/metrics.py:_profile_sort_artifact_matches_expected
            # can prove which field, value, and source drove the ordering.
            # Profile-only rank: the sort field IS the displayed field. Use
            # the canonical field_key for sort_metric_name so the normalized
            # phrase matches the authority's field_key/input_phrase set;
            # row.label carries the human title separately as metric_name.
            # sort_metric_label carries the same human title for the renderer
            # (#1495) so the lead and table never print the machine field_key.
            sort_metric_id=_PROFILE_FIELD_METRIC_IDS.get(row.field_key),
            sort_metric_name=row.field_key,
            sort_metric_label=row.label,
            # Strict-float ProfileFieldRankRow.value; the float() wrapper was a
            # no-op (see the `value=` field above) — pass it straight through.
            sort_value=row.value,
            sort_display_value=row.display_value,
            sort_academic_year=row.academic_year,
            sort_source_document=(
                allowlist[row.field_key].canonical_title
                if row.field_key in allowlist
                else "NCES district profile"
            ),
            sort_source_document_type="district_profile",
            sort_source_url=(
                allowlist[row.field_key].canonical_url
                if row.field_key in allowlist
                else None
            ),
            sort_source_valid_from=_profile_vintage_year(row),
            sort_source_valid_to=None,
        )
        for index, row in enumerate(rows, start=1)
    ]
    vintage_notes = sorted({row.vintage for row in rows if row.vintage})
    return MetricRankingResult(
        selection=selection,
        rows=result_rows,
        citations=citations,
        coverage_frame=coverage_frame_from_labels(coverage_labels),
        total_considered=len(rows),
        excluded_count=0,
        order_statement=f"Ranked by {label}, {order}.",
        source_notes=[*vintage_notes],
        methodology_codes=[
            MethodologyRef(
                code="profile_rank_uses_profile_field",
                metadata={
                    "profile_field": _profile_methodology_label(label, vintage_notes)
                },
            )
        ],
    )


def build_profile_ordered_metric_ranking_result(
    plan: QueryPlan,
    metric: MetricCandidate,
    rows: list[MetricAnswerRow],
    recent_rows: list[MetricAnswerRow],
    profile_rows: list[ProfileFieldRankRow],
    *,
    selection: ResultSelection,
    direction: str,
    reviewed_district_ids: set[int],
    academic_year: str,
    source_notes: list[str] | None = None,
) -> MetricRankingResult:
    """Build a policy-metric table ordered by an NCES profile field."""

    rows_by_district = {row.district_id: row for row in rows}
    recent_by_district = {row.district_id: row for row in recent_rows}
    order = direction_phrase(direction)
    sort_label = profile_rows[0].label if profile_rows else "profile field"
    allowlist = nces_allowlist_by_field_key()

    citation_refs: list[CitationRef] = []
    citation_marker_by_identity: dict[tuple[object, ...], int] = {}
    result_rows: list[RankingRow] = []
    coverage_labels: list[CoverageLabel] = []
    for index, profile_row in enumerate(profile_rows, start=1):
        row = rows_by_district.get(profile_row.district_id)
        recent_row = recent_by_district.get(profile_row.district_id)
        citation_markers: list[int] = []
        value: object | None = None
        row_year = academic_year
        source = "coverage_state"

        if row is None and recent_row is not None:
            coverage = coverage_label_for_stale_answer(
                recent_row.value,
                district_name=profile_row.district_name,
                metric_name=metric.name,
                current_academic_year=academic_year,
                prior_academic_year=recent_row.academic_year,
                metric_topic=_metric_topic(metric),
            )
        elif row is None:
            coverage = coverage_label_for_missing_answer(
                district_name=profile_row.district_name,
                metric_name=metric.name,
                academic_year=academic_year,
                district_has_current_year_rows=(
                    profile_row.district_id in reviewed_district_ids
                ),
                metric_topic=_metric_topic(metric),
            )
        else:
            coverage = coverage_label_for_answer(
                row.value,
                district_name=row.district_name,
                metric_name=metric.name,
                academic_year=row.academic_year,
            )
            row_year = row.academic_year
            if coverage.state == "covered":
                citation_markers = citation_markers_for_row(
                    row,
                    citation_refs,
                    citation_marker_by_identity,
                )
                value = row.value
                source = "policy_answer"

        coverage_labels.append(coverage)
        sort_source = allowlist.get(profile_row.field_key)
        result_rows.append(
            RankingRow(
                district_id=profile_row.district_id,
                district_name=profile_row.district_name,
                state=profile_row.state,
                metric_id=metric.metric_id,
                metric_name=metric.name,
                value=value,
                display_value=coverage.display,
                academic_year=row_year,
                rank=index,
                source=source,
                citation_markers=citation_markers,
                coverage_state=coverage.state,
                coverage_display=coverage.display,
                coverage_reason=coverage.reason,
                coverage_qualifier=coverage.qualifier,
                coverage_prior_academic_year=coverage.prior_academic_year,
                coverage_prior_display_value=coverage.prior_display_value,
                sort_metric_id=_PROFILE_FIELD_METRIC_IDS.get(profile_row.field_key),
                sort_metric_name=profile_row.field_key,
                # Human label for the renderer; field_key stays only on the
                # validator path (#1495).
                sort_metric_label=profile_row.label,
                # Strict-float ProfileFieldRankRow.value; the float() wrapper was
                # a no-op — pass it straight through (parse-once doctrine §8b).
                sort_value=profile_row.value,
                sort_display_value=profile_row.display_value,
                sort_academic_year=profile_row.academic_year,
                sort_source_document=(
                    sort_source.canonical_title
                    if sort_source is not None
                    else "NCES district profile"
                ),
                sort_source_document_type="district_profile",
                sort_source_url=(
                    sort_source.canonical_url if sort_source is not None else None
                ),
                sort_source_valid_from=_profile_vintage_year(profile_row),
                sort_source_valid_to=None,
            )
        )

    vintage_notes = sorted({row.vintage for row in profile_rows if row.vintage})
    return MetricRankingResult(
        selection=selection,
        rows=result_rows,
        citations=citation_refs,
        coverage_frame=coverage_frame_from_labels(coverage_labels),
        total_considered=len(profile_rows),
        excluded_count=0,
        order_statement=(
            f"Ranked by {sort_label}, {order}; displayed {metric.name}."
        ),
        source_notes=[*(source_notes or []), *vintage_notes],
        methodology_codes=[
            MethodologyRef(
                code="profile_rank_uses_profile_field",
                metadata={
                    "profile_field": _profile_methodology_label(
                        sort_label,
                        vintage_notes,
                    )
                },
            ),
            MethodologyRef(code="citation_answer_level_preferred_source_fallback"),
        ],
    )


def _profile_methodology_label(label: str, vintage_notes: list[str]) -> str:
    if not vintage_notes:
        return label
    return f"{label} ({'; '.join(vintage_notes)})"


def _profile_vintage_year(row: ProfileFieldRankRow) -> str | None:
    if row.vintage:
        match = re.search(r"\b(\d{4})\b", row.vintage)
        if match:
            return match.group(1)
    match = re.search(r"\b(\d{4})\b", row.academic_year)
    return match.group(1) if match else None


def _coverage_disclosure(
    district: SelectedDistrict,
    metric: MetricCandidate,
    academic_year: str,
    coverage: CoverageLabel,
    *,
    prior_academic_year: str | None = None,
    prior_display_value: str | None = None,
) -> CoverageDisclosure:
    return CoverageDisclosure(
        district_id=district.district_id,
        district_name=district.district_name,
        state=district.state,
        metric_id=metric.metric_id,
        metric_name=metric.name,
        academic_year=academic_year,
        coverage_state=coverage.state,
        display=coverage.display,
        reason=coverage.reason,
        qualifier=coverage.qualifier,
        prior_academic_year=prior_academic_year or coverage.prior_academic_year,
        prior_display_value=prior_display_value or coverage.prior_display_value,
    )


def _expand_ties_at_cutoff(
    candidates: list[tuple[MetricAnswerRow, float]],
    limit: int | None,
) -> tuple[list[tuple[MetricAnswerRow, float]], str | None]:
    """Return the displayed slice plus an optional tie-disclosure note (#1471).

    ``candidates`` is already sorted by the ranking key (then the deterministic
    alphabetical secondary). When a ``limit`` cuts the list at a position whose
    value is shared with the district(s) immediately below it, those tied
    districts would be silently dropped by a plain ``candidates[:limit]`` slice.
    05-accuracy-sort.md commits to disclosing ties at the cutoff rather than
    resolving them silently, so:

    - **Small tie** — include every district tied at the cutoff value and state
      it ("Districts X and Y tie at position 5; both are shown.").
    - **Large tie** — if expanding past ``limit + _MAX_TIE_EXPANSION`` would be
      needed, stop at that bound and disclose how many tied districts are not
      shown, pointing at the export, so the table stays readable.

    No tie (or no limit, or fewer candidates than the limit) returns the plain
    slice and no note.
    """

    if limit is None or len(candidates) <= limit:
        return candidates, None

    cutoff_value = candidates[limit - 1][1]
    if candidates[limit][1] != cutoff_value:
        # The boundary is clean — the row below the cutoff has a different value.
        return candidates[:limit], None

    # Districts tied at the cutoff value sit on both sides of the boundary.
    tied = [candidate for candidate in candidates if candidate[1] == cutoff_value]
    tie_count = len(tied)
    # Display the tied value from a real answer row, never a bare float (#1471).
    cutoff_display = str(tied[0][0].value)
    expansion_cap = limit + _MAX_TIE_EXPANSION
    tied_after_cut = [candidate for candidate in candidates[limit:] if candidate[1] == cutoff_value]

    if limit + len(tied_after_cut) <= expansion_cap:
        limited = candidates[:limit] + tied_after_cut
        note = (
            f"{tie_count} districts tie at position {limit} "
            f"({cutoff_display}); all tied districts are shown."
        )
        return limited, note

    limited = candidates[:expansion_cap]
    shown_at_cutoff = sum(1 for _, value in limited if value == cutoff_value)
    not_shown = tie_count - shown_at_cutoff
    note = (
        f"{tie_count} districts tie at position {limit} "
        f"({cutoff_display}); {not_shown} more tied districts are not shown "
        "here but are in the export."
    )
    return limited, note


def _limit(plan: QueryPlan) -> int | None:
    if plan.limit is None:
        return None
    if plan.limit.kind == "all":
        return None
    return plan.limit.count


# #1468: coverage states that count as "reviewed but no rankable figure" — a
# real NCTQ finding, NOT a data gap. ``not_reviewed`` is the genuine gap. The
# single authority for this set lives in ``artifacts/coverage.py`` (#1702) so the
# largest-excluded split here and the availability-block finding partition in the
# renderer read the SAME constant; the renderer reads the typed buckets rather
# than re-deriving from prose.
_REVIEWED_NO_FIGURE_STATES = REVIEWED_NO_FIGURE_STATES


def _largest_excluded_disclosure(
    *,
    selection: ResultSelection,
    selected_districts: list[SelectedDistrict],
    coverage_by_district: dict[int, CoverageLabel],
    ranked_district_ids: set[int],
) -> LargestExcludedDisclosure | None:
    """Name the larger districts left out of an "N largest by [metric]" ranking.

    Fires only for ``largest_districts`` scope (the size-attribute selection
    #1468 governs). ``selected_districts`` arrive in size order (largest first,
    from ``select_largest_districts``' enrollment ordering), so iterating in
    that order keeps the disclosure largest-first.

    A district that did NOT make the numeric-ranked set is an "excluded larger"
    district. It is sorted into:

    - ``reviewed`` — ``ina``/``na`` (NCTQ reviewed; no rankable figure). A
      finding, not a gap.
    - ``not_reviewed`` — a genuine data gap.

    ``covered`` districts that still failed to rank (e.g. a non-numeric covered
    value) are left out of both buckets: they are reviewed *and* carry a value,
    so they are neither a clean "issue not addressed" finding nor a gap, and the
    existing coverage_disclosures already account for them. ``out_of_universe``
    is likewise omitted (the issue leaves whether to mention it open; the
    conservative choice is silence).
    """

    if selection.scope != "largest_districts":
        return None

    reviewed: list[LargestExcludedDistrict] = []
    not_reviewed: list[LargestExcludedDistrict] = []
    for district in selected_districts:
        if district.district_id in ranked_district_ids:
            continue
        coverage = coverage_by_district.get(district.district_id)
        if coverage is None:
            continue
        entry = LargestExcludedDistrict(
            district_id=district.district_id,
            district_name=district.district_name,
            state=district.state,
            coverage_state=coverage.state,
        )
        if coverage.state in _REVIEWED_NO_FIGURE_STATES:
            reviewed.append(entry)
        elif coverage.state == "not_reviewed":
            not_reviewed.append(entry)

    if not reviewed and not not_reviewed:
        return None
    return LargestExcludedDisclosure(reviewed=reviewed, not_reviewed=not_reviewed)


def _scope_phrase(plan: QueryPlan) -> str:
    if plan.selection.scope in {"named_districts", "largest_districts"}:
        return " selected districts"
    states = sorted(requested_states(plan))
    if not states:
        return ""
    return f" in {', '.join(states)}"


def _ranking_result_metrics(plan: QueryPlan):
    return [
        metric
        for metric in plan.metrics
        if metric.role not in {"filter", "grouping"}
    ]


def _metric_topic(metric: MetricCandidate) -> str | None:
    topic = (metric.topic or "").strip()
    if topic:
        return topic
    metadata_topic = metric.metadata.get("topic")
    return str(metadata_topic).strip() if metadata_topic else None


def _selected_ranking_cells(
    *,
    rows: list[MetricAnswerRow],
    recent_rows: list[MetricAnswerRow],
    selected_districts: list[SelectedDistrict],
    metric: MetricCandidate,
    academic_year: str,
) -> list[tuple[SelectedDistrict, MetricAnswerRow | None, MetricAnswerRow | None]]:
    if not selected_districts:
        return [
            (
                SelectedDistrict(
                    district_id=row.district_id,
                    district_name=row.district_name,
                    state=row.state,
                ),
                row,
                None,
            )
            for row in rows
        ]

    rows_by_district = {row.district_id: row for row in rows}
    recent_by_district = {row.district_id: row for row in recent_rows}
    cells: list[
        tuple[SelectedDistrict, MetricAnswerRow | None, MetricAnswerRow | None]
    ] = []
    for district in selected_districts:
        row = rows_by_district.get(district.district_id)
        if row is None:
            cells.append((district, None, recent_by_district.get(district.district_id)))
            continue
        cells.append(
            (
                district,
                row.model_copy(
                    update={
                        "metric_id": metric.metric_id,
                        "metric_name": metric.name,
                        "academic_year": row.academic_year or academic_year,
                    }
                ),
                None,
            )
        )
    return cells
