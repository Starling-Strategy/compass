"""Deterministic NCES/profile lookup operation path."""

from __future__ import annotations

from collections.abc import Sequence

from compass_backend.artifacts import (
    CitationRef,
    CoverageLabel,
    MethodologyRef,
    ProfileLookupResult,
    ProfileValueRow,
    ResultSelection,
    SelectedDistrict,
    coverage_frame_from_labels,
)
from compass_backend.catalog import NCESFieldCandidate, ProfileFieldValueRow
from compass_backend.catalog.nces_allowlist import nces_allowlist_by_field_key
from compass_backend.contracts.planning import QueryPlan

from .evidence import _citation_identity
from .selection import selection_filters_are_supported


# Honest cell + coverage display for a resolved district that the NCES profile
# lookup returned no clean value for (the lookup drops a name with 0 or >1
# nces_districts matches). Shown verbatim because ``coverage_reason="unavailable"``
# has no short label (artifacts/coverage.py::_SHORT_LABEL_BY_REASON), so the
# renderer's ``_cell_display_value`` falls back to this string.
_PROFILE_NO_DATA_DISPLAY = "No NCES data"


def profile_lookup_shape_is_supported(plan: QueryPlan) -> bool:
    """Return whether the profile lookup path supports this query plan."""

    return (
        plan.operation == "profile_lookup"
        and plan.selection.scope == "named_districts"
        and bool(plan.profile_fields)
        and plan.sort is None
        and not plan.sort_steps
        and plan.limit is None
        and selection_filters_are_supported(plan)
    )


def build_profile_lookup_result(
    plan: QueryPlan,
    field: NCESFieldCandidate | Sequence[NCESFieldCandidate],
    rows: list[ProfileFieldValueRow],
    *,
    academic_year: str,
    resolved_selection: ResultSelection | None = None,
) -> ProfileLookupResult:
    """Build an exact NCES/profile lookup result from resolved field authority.

    Per W2-M5-03 (#747): every row carries an NCES citation pulled from
    the governed allowlist (migration 060 / W2-M5-00a). One CitationRef
    shared across all rows that cite the same NCES URL (W2-M5-04 URL-dedup
    pattern) — including across *different* field keys, since several NCES
    fields (enrollment, teachers_fte, pupil_teacher_ratio, …) resolve to the
    same ELSI page. Dedup is keyed on the shared ``_citation_identity`` (URL,
    title-fallback) used by ``evidence.py``/``ranking.py``, not on
    ``field_key`` — keying on ``field_key`` re-emitted the same URL once per
    field and produced duplicate Sources entries (#1567).

    Value-or-null over the resolved selection (cross-turn follow-up fix): when
    ``resolved_selection`` is passed, the result carries EVERY resolved district
    (value-or-null), not only the districts the NCES name-match kept. The lookup
    (``db/catalog.py::lookup_nces_profile_values``) silently drops a requested
    name with 0 or >1 ``nces_districts`` matches, so "enrollment for those
    districts" over [Denver, Dallas, Buffalo] returned 2 rows — ``result.selection``
    then lost Buffalo/NY and the independent ``intended_geography`` Gate-3 saw NY
    uncovered and BLOCKED the whole turn. Emitting one row per (resolved district ×
    field) — a covered value where the lookup found one, an honest "no NCES data"
    cell otherwise — keeps ``resolved_states`` complete so that validator goes
    silent as a consequence (no change to the grounding guard). This applies only
    when ``resolved_selection`` is given AND ``rows`` is non-empty; the all-empty
    case stays on ``operations.py::_handle_profile_lookup_empty_rows`` (#1758).
    """

    coverage_labels: list[CoverageLabel] = []
    fields = [field] if isinstance(field, NCESFieldCandidate) else list(field)
    allowlist = nces_allowlist_by_field_key()
    citations: list[CitationRef] = []
    marker_by_field_key: dict[str, int] = {}
    marker_by_identity: dict[tuple[object, ...], int] = {}
    result_rows: list[ProfileValueRow] = []

    def _markers_for_covered_field(field_key: str, field_academic_year: str) -> list[int]:
        """Register (URL-deduped) the NCES citation for a covered field, return markers."""

        entry = allowlist.get(field_key)
        if entry is not None and field_key not in marker_by_field_key:
            candidate = CitationRef(
                marker=len(citations) + 1,
                title=entry.canonical_title,
                url=entry.canonical_url,
                academic_year=field_academic_year,
                document_type="NCES profile field",
            )
            identity = _citation_identity(candidate)
            existing_marker = marker_by_identity.get(identity)
            if existing_marker is None:
                marker_by_identity[identity] = candidate.marker
                marker_by_field_key[field_key] = candidate.marker
                citations.append(candidate)
            else:
                # A different field key already cited this URL — share its
                # marker instead of appending a duplicate Sources entry.
                marker_by_field_key[field_key] = existing_marker
        return [marker_by_field_key[field_key]] if field_key in marker_by_field_key else []

    def _covered_row(row: ProfileFieldValueRow) -> ProfileValueRow:
        coverage = CoverageLabel(
            state="covered",
            display=row.display_value,
            raw_value=row.value,
            reason="profile_field_value",
        )
        coverage_labels.append(coverage)
        return ProfileValueRow(
            district_id=row.district_id,
            district_name=row.district_name,
            state=row.state,
            field_key=row.field_key,
            label=row.label,
            value=row.value,
            display_value=row.display_value,
            academic_year=row.academic_year,
            source_label=row.source_label,
            provenance=row.provenance,
            covered_by_compass=row.covered_by_compass,
            metric_name=row.label,
            citation_markers=_markers_for_covered_field(row.field_key, row.academic_year),
            coverage_state=coverage.state,
            coverage_display=coverage.display,
            coverage_reason=coverage.reason,
            coverage_qualifier=coverage.qualifier,
        )

    def _no_data_row(
        district: SelectedDistrict, field_candidate: NCESFieldCandidate
    ) -> ProfileValueRow:
        # Non-covered, honest "no NCES data" cell. State "na" keeps the row a
        # rendered answer-state row so the missing district stays VISIBLE in both
        # the chat table and the CSV (named, with a clear "No NCES data" cell)
        # rather than vanishing. Reason "unavailable" is the precise data-gap
        # channel (CoverageBreakdown.unavailable_count): the district-coverage
        # availability summary (district_coverage_summary_for_rows) treats a
        # reason=="unavailable" cell as a GAP, not "current reviewed data", so the
        # D9 sentence stays honest ("2 have current data; 1 hasn't been reviewed")
        # instead of overstating it. Keeps coverage_display == display_value for
        # the coverage-state validator.
        coverage = CoverageLabel(
            state="na",
            display=_PROFILE_NO_DATA_DISPLAY,
            reason="unavailable",
        )
        coverage_labels.append(coverage)
        return ProfileValueRow(
            district_id=district.district_id,
            district_name=district.district_name,
            state=district.state,
            field_key=field_candidate.field_key,
            label=field_candidate.label,
            value=None,
            display_value=_PROFILE_NO_DATA_DISPLAY,
            academic_year=academic_year,
            source_label="NCES district profile",
            provenance=f"No NCES profile value matched for {district.district_name}.",
            covered_by_compass=False,
            metric_name=field_candidate.label,
            citation_markers=[],
            coverage_state=coverage.state,
            coverage_display=coverage.display,
            coverage_reason=coverage.reason,
            coverage_qualifier=coverage.qualifier,
        )

    use_resolved_selection = resolved_selection is not None and bool(rows)
    if use_resolved_selection:
        # One row per (resolved district × field): a covered value where the
        # lookup returned one (matched by district_id AND field_key), an honest
        # no-data cell otherwise. Truly-unresolved NCES-only names (district_id
        # is None) keep the existing covered-row + unresolved_districts handling.
        covered_by_key: dict[tuple[int, str], ProfileFieldValueRow] = {}
        unresolved_rows: list[ProfileFieldValueRow] = []
        for row in rows:
            if row.district_id is None:
                unresolved_rows.append(row)
                continue
            covered_by_key.setdefault((row.district_id, row.field_key), row)
        for district in resolved_selection.districts:
            for field_candidate in fields:
                covered = covered_by_key.get(
                    (district.district_id, field_candidate.field_key)
                )
                result_rows.append(
                    _covered_row(covered)
                    if covered is not None
                    else _no_data_row(district, field_candidate)
                )
        result_rows.extend(_covered_row(row) for row in unresolved_rows)
    else:
        result_rows.extend(_covered_row(row) for row in rows)

    selected_districts: list[SelectedDistrict] = []
    unresolved_districts: list[str] = []
    seen_unresolved_names: set[str] = set()
    if use_resolved_selection:
        selected_districts = [
            SelectedDistrict(
                district_id=district.district_id,
                district_name=district.district_name,
                state=district.state,
            )
            for district in resolved_selection.districts
        ]
        for row in rows:
            if row.district_id is None and row.district_name not in seen_unresolved_names:
                seen_unresolved_names.add(row.district_name)
                unresolved_districts.append(row.district_name)
    else:
        seen_district_ids: set[int] = set()
        for row in rows:
            if row.district_id is None:
                if row.district_name not in seen_unresolved_names:
                    seen_unresolved_names.add(row.district_name)
                    unresolved_districts.append(row.district_name)
                continue
            if row.district_id in seen_district_ids:
                continue
            seen_district_ids.add(row.district_id)
            selected_districts.append(
                SelectedDistrict(
                    district_id=row.district_id,
                    district_name=row.district_name,
                    state=row.state,
                )
            )

    selection = ResultSelection(
        scope="named_districts",
        districts=selected_districts,
        unresolved_districts=unresolved_districts,
        states=plan.selection.states,
    )
    if len(fields) == 1:
        order_statement = (
            f"Looked up {fields[0].label} from NCES/profile fields for selected "
            f"districts in {academic_year}."
        )
    else:
        order_statement = (
            "Looked up selected NCES/profile fields for selected districts in "
            f"{academic_year}."
        )
    return ProfileLookupResult(
        selection=selection,
        rows=result_rows,
        citations=citations,
        coverage_frame=coverage_frame_from_labels(coverage_labels),
        total_considered=len(result_rows),
        excluded_count=0,
        order_statement=order_statement,
        methodology_codes=[
            MethodologyRef(code="profile_lookup_approved_field"),
            MethodologyRef(code="profile_lookup_compass_coverage_flag"),
        ],
    )
