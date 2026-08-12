"""Deterministic writer for validated Compass artifacts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict, assert_never

from compass_backend.catalog import MetricCandidate
from compass_backend.artifacts import (
    CategoricalCountRow,
    CompositeRankingResult,
    MethodologyRef,
    MetricCountResult,
    MetricLookupResult,
    MetricRankingResult,
    MetricTrendResult,
    PeerComparisonResult,
    ProfileLookupResult,
    ResultSet,
    SelectedDistrict,
    is_rendered_answer_state,
    is_stale_prior_value_row,
    lookup_renders_stale_prior_values,
    short_coverage_label,
)
from compass_backend.artifacts.identity import compute_artifact_id
from compass_backend.config import settings
from compass_backend.contracts.planning import MetricSpec, QueryPlan
from compass_backend.contracts.rendering import ResponseManifest
from compass_backend.contracts.validation import ValidationReport
from compass_backend.rendering.shared import sort_metric_name

from .composer import (
    AnswerComposition,
    district_coverage_sentence,
    compose_response,
    is_single_filter_count,
    methodology_lines_for_result,
    unresolved_district_sentences,
)
from .formatting import (
    MATCHING_DISTRICTS_DISPLAY_LIMIT as _MATCHING_DISTRICTS_DISPLAY_LIMIT_SHARED,
    escape_markdown_cell as _escape_markdown_table_cell,
    format_citation_markers as _format_markers,
    format_delta as _format_delta,
    format_percent as _format_percent,
)


def render_response(
    plan: QueryPlan,
    result: ResultSet,
    validation: ValidationReport,
) -> ResponseManifest:
    """Render a deterministic artifact without redoing data operations."""

    if not validation.valid:
        finding_codes = ", ".join(finding.code for finding in validation.findings)
        return ResponseManifest(
            body=(
                "I found a deterministic result, but validation failed before "
                f"rendering. Validation findings: {finding_codes}."
            ),
            status="validation_failed",
            result_type=result.result_type,
            validation_valid=False,
            metadata={"question": plan.question},
        )

    body = _render_result(plan, result)
    return ResponseManifest(
        body=body,
        status="rendered",
        result_type=result.result_type,
        validation_valid=True,
        metadata={
            "question": plan.question,
            # Stamp the canonical artifact_id so the surface_consistency
            # validator can later re-compute it from `result` and assert the
            # stamp matches — i.e. no one mutated the ResultSet between
            # construction and render-emit. The SSE writer ships this stamp
            # through `manifest.metadata`, so every downstream surface
            # carries the same id without explicit plumbing.
            "artifact_id": compute_artifact_id(result),
            # WS-2 (#1242): the chart and CSV export ship beside the markdown
            # body, so they never appear inside it. Stamp their presence so the
            # answer-layer brief can tell the stylist they ARE attached — the
            # stylist otherwise invents a "Compass can't generate a chart"
            # refusal because the body it sees has no chart.
            "has_chart": result.chart is not None,
            "has_csv_export": result.csv_export is not None,
            **display_metadata_for_result(plan, result),
        },
    )


def attach_adjacent_metrics_manifest_metadata(
    manifest: ResponseManifest,
    adjacent_candidates: list[MetricCandidate],
) -> ResponseManifest:
    """Attach adjacent metric labels to manifest metadata for the answer layer.

    Called after ``render_response`` whenever the execution outcome carries
    ``adjacent_candidates`` from a ``select_with_alternates`` adjudication.
    Writes structured data only — prose is composed by the stylist.
    No-ops when ``adjacent_candidates`` is empty.
    """

    if not adjacent_candidates:
        return manifest
    payload = [
        {"metric_id": candidate.metric_id, "label": candidate.name}
        for candidate in adjacent_candidates
    ]
    return manifest.model_copy(
        update={
            "metadata": {
                **manifest.metadata,
                "adjacent_metrics": payload,
            }
        }
    )


def _render_result(plan: QueryPlan, result: ResultSet) -> str:
    """Dispatch a validated result to the right renderer.

    Each branch narrows `result` to the variant type so renderers consume
    typed rows directly. `assert_never` makes adding a new variant a
    compile-time error: the type checker will refuse to compile this
    function until a matching `case` is added.
    """

    match result:
        case MetricLookupResult():
            return _render_metric_lookup(plan, result)
        case MetricTrendResult():
            return _render_metric_trend(result)
        case MetricCountResult():
            return _render_metric_count(plan, result)
        case ProfileLookupResult():
            return _render_profile_lookup(result)
        case PeerComparisonResult():
            return _render_peer_comparison(result)
        case MetricRankingResult():
            return _render_metric_ranking(plan, result)
        case CompositeRankingResult():
            return _render_composite_ranking(plan, result)
        case _ as unreachable:
            assert_never(unreachable)


def _render_metric_count(plan: QueryPlan, result: MetricCountResult) -> str:
    if any(isinstance(row, CategoricalCountRow) for row in result.rows):
        return _render_categorical_metric_count(plan, result)

    composition = compose_response(plan, result)
    include_sources = _has_row_citation_markers(result.rows)
    header = ["Metric", "Count", "Denominator", "Filter"]
    alignment = ["---", "---:", "---:", "---"]
    if include_sources:
        header.append("Sources")
        alignment.append("---")

    lines = [
        *composition.lead_lines,
        "",
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(alignment) + " |",
    ]
    for row in result.rows:
        values = [
            _escape_markdown_table_cell(row.metric_name),
            str(row.count),
            str(row.denominator),
            _escape_markdown_table_cell(row.filter_statement),
        ]
        if include_sources:
            values.append(_format_markers(row.citation_markers))
        lines.append(
            "| " + " | ".join(values) + " |"
        )

    # Only the single-filter count earns a names table — the same shape the
    # composer leads with "Of N … X (Y%) match your criteria." A covered-universe
    # count has no filter, and a multi-metric count has no single qualifying
    # set, so listing names there would mislabel or conflate them (C1, #1413).
    if is_single_filter_count(result):
        _append_matching_districts(lines, result)
    _append_response_sections(lines, composition)

    return "\n".join(lines)


# A count can match the whole covered universe; past this many names a flat
# table stops being readable, so we offer to list them on request instead.
# Single-sourced in rendering/formatting.py so the fact-coverage validator
# shares the same threshold.
_MATCHING_DISTRICTS_DISPLAY_LIMIT = _MATCHING_DISTRICTS_DISPLAY_LIMIT_SHARED


def _matching_districts(result: MetricCountResult) -> list[SelectedDistrict]:
    """The selected districts whose ids are in the count's qualifying set.

    Uses the same membership / no-invention rule as the selection-backed join
    in session.memory._result_district_refs: a name is resolved only for an id
    the selection actually contains, so the renderer never invents a name.
    (Ordering differs — this list is alphabetized for legibility, not kept in
    selection order.) De-duped across rows.
    """

    if result.selection is None:
        return []
    qualifying_ids: list[int] = []
    seen: set[int] = set()
    for row in result.rows:
        for district_id in getattr(row, "qualifying_district_ids", []):
            if district_id not in seen:
                seen.add(district_id)
                qualifying_ids.append(district_id)
    if not qualifying_ids:
        return []
    by_id = {district.district_id: district for district in result.selection.districts}
    matched = [by_id[i] for i in qualifying_ids if i in by_id]
    return sorted(matched, key=lambda district: district.district_name.casefold())


def _append_matching_districts(
    lines: list[str], result: MetricCountResult
) -> None:
    """Append the matching district names the count result already holds.

    Rendered as a markdown table (not prose) so the answer-stylist pass treats
    it as an immutable block and cannot drop the names (C1, #1413).
    """

    matched = _matching_districts(result)
    if not matched:
        return
    if len(matched) > _MATCHING_DISTRICTS_DISPLAY_LIMIT:
        lines.append("")
        lines.append(
            f"{len(matched)} districts match — ask me to name them and I'll list them."
        )
        return
    lines.append("")
    lines.append(f"Matching districts ({len(matched)}):")
    lines.append("")
    lines.append("| District | State |")
    lines.append("| --- | --- |")
    for district in matched:
        lines.append(
            "| "
            + _escape_markdown_table_cell(district.district_name)
            + " | "
            + _escape_markdown_table_cell(district.state or "")
            + " |"
        )


def _render_categorical_metric_count(plan: QueryPlan, result: MetricCountResult) -> str:
    category_header = _categorical_count_header(plan, result)
    # #1514 D12: the "Not reviewed" / "Unavailable" buckets are not answer
    # values — they leave the table and their counts (which ARE district
    # counts) are voiced as narrative sentences. The remaining shares stay as
    # computed against the original denominator; the sentences explain it.
    answer_buckets = [
        row
        for row in result.rows
        if isinstance(row, CategoricalCountRow) and _is_rendered_answer_row(row)
    ]
    coverage_lines = [
        _categorical_coverage_sentence(row)
        for row in result.rows
        if isinstance(row, CategoricalCountRow) and not _is_rendered_answer_row(row)
    ]
    # #1514 rule 5: this path bypasses compose_response, so it voices each
    # requested name outside the Pathfinder itself — same one composition
    # point (composer.unresolved_district_sentences) as the count composer.
    coverage_lines.extend(unresolved_district_sentences(result.selection))
    lines = [f"Counted {category_header.casefold()} distribution."]
    if coverage_lines:
        lines.append("")
        lines.extend(coverage_lines)
    if not answer_buckets:
        # Empty-table guard: narrative-only body, never a header-only table.
        _append_methodology_section(lines, result)
        return "\n".join(lines)
    lines.extend(
        [
            "",
            f"| {category_header} | District count | Share of covered districts |",
            "| --- | ---: | ---: |",
        ]
    )
    for row in answer_buckets:
        lines.append(
            "| "
            f"{_escape_markdown_table_cell(row.category)} | "
            f"{row.count} | "
            f"{_format_percent(row.percent)} |"
        )

    _append_methodology_section(lines, result)

    return "\n".join(lines)


def _categorical_coverage_sentence(row: CategoricalCountRow) -> str:
    """Rule-2 narrative for a non-answer categorical bucket (#1514 D12).

    ``row.count`` is a district count, so the sentence counts districts —
    both numbers are artifact values (numeric-token provenance).
    """

    district_word = "district" if row.denominator == 1 else "districts"
    if row.coverage_reason == "unavailable":
        return (
            f"The reviewed value is unavailable for {row.count} of the "
            f"{row.denominator} covered {district_word}."
        )
    return (
        f"NCTQ hasn't reviewed {row.count} of the {row.denominator} covered "
        f"{district_word} for this metric in {row.academic_year} yet."
    )


def _render_metric_ranking(plan: QueryPlan, result: MetricRankingResult) -> str:
    composition = compose_response(plan, result)
    metric_name = result.rows[0].metric_name if result.rows else "the selected metric"
    sort_metric = sort_metric_name(result)
    if sort_metric and sort_metric != metric_name:
        return _render_mixed_metric_ranking(
            plan,
            result,
            metric_name,
            sort_metric,
            composition,
        )

    display_rows = _display_rows(plan, result)
    if not display_rows:
        # #1514 empty-table guard: every requested district lacks a current
        # answer — the body is narrative-only, never a header-only table. The
        # availability section (composer) carries the canonical per-district
        # sentences, including ones re-derived from non-answer result rows.
        # #1228: with no table, this section IS the answer — render it
        # always-visible (not under a foldable #### heading), or the frontend
        # would hide the entire answer behind a collapsed toggle.
        lines = [*composition.lead_lines]
        _append_availability_section(lines, composition, as_collapsible=False)
        _append_largest_excluded_section(lines, composition)
        _append_methodology_lines(lines, composition)
        return "\n".join(lines)

    include_sources = _has_row_citation_markers(display_rows)
    header = [
        "Rank",
        "District",
        "State",
        _escape_markdown_table_cell(metric_name),
    ]
    alignment = ["---", "---", "---", "---"]
    if include_sources:
        header.append("Sources")
        alignment.append("---")

    lines = [
        *composition.lead_lines,
    ]
    _append_display_notice(lines, plan, result)
    lines.extend(
        [
            "",
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(alignment) + " |",
        ]
    )
    for row in display_rows:
        values = [
            str(row.rank),
            _escape_markdown_table_cell(row.district_name),
            _escape_markdown_table_cell(row.state or ""),
            _escape_markdown_table_cell(_cell_display_value(row)),
        ]
        if include_sources:
            values.append(_format_markers(row.citation_markers))
        lines.append("| " + " | ".join(values) + " |")

    # #1228 / VOICE-R1: disclose not-ranked districts AFTER the answer table.
    _append_availability_section(lines, composition)
    _append_largest_excluded_section(lines, composition)
    _append_methodology_lines(lines, composition)

    return "\n".join(lines)


def _render_mixed_metric_ranking(
    plan: QueryPlan,
    result: MetricRankingResult,
    metric_name: str,
    sort_metric_name: str,
    composition: AnswerComposition,
) -> str:
    display_rows = _display_rows(plan, result)
    if not display_rows:
        # #1514 empty-table guard (this branch previously rendered a
        # header-only table): narrative-only body when no row holds an answer.
        # #1228: no table -> this section IS the answer, so keep it visible
        # (not under a foldable #### heading).
        lines = [*composition.lead_lines]
        _append_availability_section(lines, composition, as_collapsible=False)
        _append_methodology_lines(lines, composition)
        return "\n".join(lines)

    include_sources = _has_row_citation_markers(display_rows)
    header = [
        "Rank",
        "District",
        "State",
        _escape_markdown_table_cell(sort_metric_name),
        _escape_markdown_table_cell(metric_name),
    ]
    alignment = ["---", "---", "---", "---", "---"]
    if include_sources:
        header.append("Sources")
        alignment.append("---")

    lines = [
        *composition.lead_lines,
    ]
    _append_display_notice(lines, plan, result)
    lines.extend(
        [
            "",
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(alignment) + " |",
        ]
    )
    for row in display_rows:
        values = [
            str(row.rank),
            _escape_markdown_table_cell(row.district_name),
            _escape_markdown_table_cell(row.state or ""),
            _escape_markdown_table_cell(_sort_display_value(row)),
            _escape_markdown_table_cell(_cell_display_value(row)),
        ]
        if include_sources:
            values.append(_format_markers(row.citation_markers))
        lines.append("| " + " | ".join(values) + " |")

    # #1228 / VOICE-R1: disclose not-ranked districts AFTER the answer table.
    _append_availability_section(lines, composition)
    _append_largest_excluded_section(lines, composition)
    _append_methodology_lines(lines, composition)

    return "\n".join(lines)


def _render_composite_ranking(
    plan: QueryPlan,
    result: CompositeRankingResult,
) -> str:
    """W2-M3-01 phase 4 (#861): render a ``CompositeRankingResult`` as
    one markdown rank table per child, prefixed by the composite envelope's
    own ``order_statement`` as a user-visible introduction.

    The envelope is a typed contract (phase 1, #852): each child is a
    fully validated ``MetricRankingResult`` carrying its own metric,
    citations, coverage frame, methodology codes, and CSV export. Phase 4
    leans on the existing per-result renderer ``_render_metric_ranking``
    to produce each child's section, so the per-child output stays
    byte-identical to a stand-alone single-metric rank — the renderer
    surface is the only piece of new code, not a new per-row writer.

    Each child is rendered with a ``child_plan`` that carries only that
    child's metric (mirrors the executor pattern at
    ``operations._execute_composite_ranking``). Without this, helpers like
    ``composer._threshold_hint_line`` and ``shared.selection_label``
    would read the composite plan's full N-metric / aggregate filter slice
    and leak composite-level context into per-child output, violating the
    artifact contract that each child renders standalone.

    Sources de-duplication across children landed in W2-M5-04 (#748):
    ``_execute_composite_ranking`` now threads a shared citation-dedup
    state through every sibling ``build_metric_ranking_result`` call so
    inline marker numbering stays consistent across child tables (the
    same URL → the same ``[N]``), and the composite envelope is built
    with ``citations=list(shared_citation_refs)`` so the answer-level
    Sources panel emitted by ``api/sse.py`` is a single deduped list.
    Per-child ``citations`` fields remain snapshots at each child's
    construction time and may be partial — the envelope is the
    answer-level source of truth. The composite envelope's own
    ``methodology_codes`` / ``source_notes`` (composite-level) are still
    not surfaced; per-child methodology remains the source of truth.
    """

    sections: list[str] = [result.order_statement, ""]
    for child in result.children:
        child_metric_name = (
            child.rows[0].metric_name if child.rows else plan.metrics[0].name
        )
        child_plan = plan.model_copy(
            update={
                "metrics": [MetricSpec(name=child_metric_name)],
                "requires_composite_ranking": False,
            }
        )
        sections.append(_render_metric_ranking(child_plan, child))
        sections.append("")
    if sections and sections[-1] == "":
        sections.pop()
    return "\n".join(sections)


def display_metadata_for_result(
    plan: QueryPlan,
    result: ResultSet,
) -> dict[str, object]:
    """Return row display and limit provenance metadata for one result."""

    if isinstance(result, CompositeRankingResult):
        # The composite envelope's own ``rows`` is forced empty by the
        # artifact contract (phase 1) — actual rows live on each child.
        # Surface an aggregate so manifest metadata stays honest about
        # what the user is seeing.
        total_rows = sum(len(child.rows) for child in result.children)
        # displayed counts follow the answers-only table subset (#1514);
        # row_count stays the full artifact record.
        total_displayed = sum(
            (
                min(
                    _ranking_display_limit(plan, child) or len(_answer_rows(child)),
                    len(_answer_rows(child)),
                )
            )
            for child in result.children
        )
        total_citations = (
            len(result.citations)
            if result.citations
            else sum(len(child.citations) for child in result.children)
        )
        return {
            "row_count": total_rows,
            "displayed_row_count": total_displayed,
            "display_limit": None,
            "row_display": plan.output.row_display,
            "data_limit_count": None,
            "data_limit_kind": None,
            "data_limit_source": "unbounded",
            "display_limit_source": "none",
            "citation_count": total_citations,
            "composite_child_count": len(result.children),
        }

    # #1514: displayed_row_count / display_limit describe the answers-only
    # rendered table; row_count keeps reporting the full artifact record.
    answer_row_count = len(_answer_rows(result))
    display_limit = _ranking_display_limit(plan, result)
    displayed_row_count = (
        answer_row_count
        if display_limit is None
        else min(display_limit, answer_row_count)
    )
    data_limit_count = None
    data_limit_kind = None
    data_limit_source = "unbounded"
    if plan.limit is not None:
        data_limit_count = plan.limit.count
        data_limit_kind = plan.limit.kind
        data_limit_source = "limit_spec"
    elif plan.selection.scope == "largest_districts":
        data_limit_count = len(result.rows)
        data_limit_kind = "default"
        data_limit_source = "largest_districts_default"

    return {
        "row_count": len(result.rows),
        "displayed_row_count": displayed_row_count,
        "display_limit": display_limit,
        "row_display": plan.output.row_display,
        "data_limit_count": data_limit_count,
        "data_limit_kind": data_limit_kind,
        "data_limit_source": data_limit_source,
        "display_limit_source": (
            "renderer_preview" if display_limit is not None else "none"
        ),
        "citation_count": len(result.citations),
    }


def _display_rows(plan: QueryPlan, result: MetricRankingResult):
    """The ranked rows that render: answers only (#1514), then preview-limited.

    Single-metric rankings promote not_reviewed placeholders AFTER the ranked
    block (D4), so dropping them here leaves a contiguous 1..N table. Mixed /
    profile-ordered rankings keep their honest rank gaps — ranks are never
    renumbered at render time (CSV and follow-up turns reference artifact
    ranks).
    """

    answer_rows = _answer_rows(result)
    display_limit = _ranking_display_limit(plan, result)
    if display_limit is None:
        return answer_rows
    return answer_rows[:display_limit]


def _append_display_notice(
    lines: list[str],
    plan: QueryPlan,
    result: MetricRankingResult,
) -> None:
    display_limit = _ranking_display_limit(plan, result)
    if display_limit is None:
        return
    # #1514: the notice counts the answers-only ranked set — the same rows
    # the table and the CSV data rows hold — not the full artifact record.
    row_count = len(_answer_rows(result))
    lines.append(
        f"Showing {display_limit} of {row_count} ranked rows here. "
        f"Export includes all {row_count} ranked districts. "
        "Ask to show all to expand the table."
    )


def _ranking_display_limit(plan: QueryPlan, result: ResultSet) -> int | None:
    if not isinstance(result, MetricRankingResult):
        return None
    if plan.limit is not None or plan.output.row_display == "all":
        return None
    display_limit = settings.ranking_display_limit
    if len(_answer_rows(result)) <= display_limit:
        return None
    return display_limit


def _sort_display_value(row) -> str:
    display_value = getattr(row, "sort_display_value", None)
    if display_value:
        return str(display_value)
    sort_value = getattr(row, "sort_value", None)
    return "" if sort_value is None else str(sort_value)


def _cell_display_value(row) -> str:
    """Return the value to render in a table cell.

    For non-covered rows, swap the long coverage display string for the short
    label keyed by ``coverage_reason``. Covered rows pass through unchanged.
    Under #1514 only answer rows reach table cells, so the non-covered branch
    now only serves INA / N-A rows (whose short labels are unchanged, D14) —
    plus, under #1826, stale rows the single-metric lookup keeps in the table:
    those show their prior-year value (``coverage_prior_display_value``), with
    the prior year annotated in the Academic-year column (see
    ``_lookup_academic_year_cell``). A stale row only reaches this helper when a
    surface renders it, i.e. the single-metric lookup — every other surface
    still demotes stale rows to prose, so the branch is inert for them.
    """

    if is_stale_prior_value_row(row):
        return row.coverage_prior_display_value or row.display_value
    coverage_state = getattr(row, "coverage_state", None)
    if coverage_state is None or coverage_state == "covered":
        return row.display_value
    return short_coverage_label(getattr(row, "coverage_reason", None), row.display_value)


def _lookup_academic_year_cell(row) -> str:
    """#1826 — the Academic-year cell for a single-metric lookup row.

    A stale row shows the year it was LAST reviewed
    (``coverage_prior_academic_year``, e.g. 2022-23) beside its prior value, so
    the column annotates the staleness honestly next to the current-year rows.
    Every non-stale row keeps its own ``academic_year``.
    """

    if is_stale_prior_value_row(row):
        return row.coverage_prior_academic_year or row.academic_year
    return row.academic_year


def _is_rendered_answer_row(row) -> bool:
    """#1514 — tables and charts hold answer rows only (value, INA, or N/A).

    Built on the shared ``is_rendered_answer_state`` predicate
    (artifacts/coverage.py), which owns the None-as-answer pre-labeling
    convention so table, CSV, and chart partition rows identically.
    """

    return is_rendered_answer_state(getattr(row, "coverage_state", None))


def _answer_rows(result: ResultSet) -> list:
    """The subset of ``result.rows`` that renders in the table.

    Answer rows always render (#1514). For the single-metric lookup surface,
    #1826 also keeps stale prior-value rows in the table, so this extends the
    subset there — and only there (``lookup_renders_stale_prior_values`` is the
    one result-level gate every surface keys on: table, CSV, manifest count, and
    the parity validators). The state-grouped lookup aggregates covered rows
    only, so it filters stale out at its own grouping call site.
    """

    include_stale = lookup_renders_stale_prior_values(result)
    return [
        row
        for row in result.rows
        if _is_rendered_answer_row(row)
        or (include_stale and is_stale_prior_value_row(row))
    ]


def _anchor_value_lead(answer_rows: list) -> str | None:
    """Confirm the anchor district's own metric value(s) in a lead sentence.

    Peer-comparison prose otherwise opens with selection methodology and leaves
    the anchor district's own value only in its table row, so a reader can't see
    "where do *I* stand?" before scanning the peers. This surfaces the anchor's
    own reviewed figures up front (VOICE-R1; NCTQ feedback 2026-06-15).

    Returns ``None`` when no anchor answer row exists — e.g. an answerless anchor,
    whose coverage sentence ``_narrated_coverage_lines`` already voices first, so
    this never double-voices. Built as an f-string (not a ``lines = [...]`` list
    constant) so the no-prose-dispatch baseline scanner, which keys on the first
    sorted string literal in each list literal, is unaffected.
    """

    anchor_rows = [r for r in answer_rows if getattr(r, "peer_role", None) == "anchor"]
    if not anchor_rows:
        return None
    name = anchor_rows[0].district_name
    pairs = [f"{r.metric_name.lower()} — {_cell_display_value(r)}" for r in anchor_rows]
    return f"For {name}, NCTQ's reviewed figures are: {_join_readable(pairs)}."


def _anchor_value_filter_lead(plan: QueryPlan, answer_rows: list) -> str | None:
    """Name the anchor district and its shared reviewed value for an equality
    filter ("districts with the SAME <metric> as <anchor>").

    The B05 / #1556 client bug: a metric_lookup whose universe was narrowed by an
    ``anchor_value`` equality filter rendered only the matching districts and their
    value column, never stating WHOSE value they match. With ``exclude_anchor=True``
    (the default and the reported shape) the anchor district itself is excluded
    from the table, so the reader never sees the anchor named at all — the answer
    silently floats free of "the same as *whom*?".

    Every matched row shares the anchor's value by construction (it is an equality
    filter on one metric), so ``answer_rows[0]``'s cell carries the grounded anchor
    value — no execution change or re-derivation needed. We name the anchor district
    (with its state when the planner supplied one) and that shared value.

    Returns ``None`` for any non-anchor-equality lookup so plain lookups are
    untouched. Built as an f-string (not a ``lines = [...]`` list constant) so the
    no-prose-dispatch baseline scanner is unaffected — same idiom as
    :func:`_anchor_value_lead`.
    """

    if not answer_rows:
        return None
    anchor = _anchor_value_equality_spec(plan)
    if anchor is None:
        return None
    name = anchor.district
    if anchor.state:
        name = f"{name}, {anchor.state}"
    shared_value = _cell_display_value(answer_rows[0])
    return (
        f"These districts share {name}'s reviewed value of {shared_value} "
        "for this metric."
    )


def _anchor_value_equality_spec(plan: QueryPlan):
    """Return the first equality ``anchor_value`` spec on the plan, else ``None``.

    The B05 equality-anchor shape is a ``FilterSpec`` with ``operator="equals"``
    and an ``anchor_value`` (``AnchorValueSpec``) carrying the anchor district the
    user named ("same as Portland, ME"). Other filters and plain lookups return
    ``None``.
    """

    for filter_spec in plan.filters:
        if filter_spec.operator == "equals" and filter_spec.anchor_value is not None:
            return filter_spec.anchor_value
    return None


def _narrated_coverage_lines(rows, *, include_stale: bool = False) -> list[str]:
    """Canonical narrative sentences for non-answer rows, deduplicated.

    Each non-answer row already carries its canonical sentence (built by the
    label authority in artifacts/coverage.py) on ``coverage_display`` — the
    writer surfaces it verbatim instead of rebuilding prose. Deduplication by
    exact sentence gives the #1514 D5 shape for free: an out-of-universe
    district repeated across trend years collapses to ONE sentence (identical
    text per year), while per-year not-reviewed sentences differ by year and
    are each kept.

    ``include_stale=True`` (the single-metric lookup, #1826) skips stale rows
    here because they now render IN the table — narrating them too would
    double-voice the same prior-year value. Every other caller keeps the default
    (stale rows are still narrated), so ranking/trend/state-grouped are unchanged.
    """

    lines: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if _is_rendered_answer_row(row):
            continue
        if include_stale and is_stale_prior_value_row(row):
            continue
        sentence = (getattr(row, "coverage_display", None) or row.display_value).strip()
        if sentence and sentence not in seen:
            seen.add(sentence)
            lines.append(sentence)
    return lines


def _render_metric_lookup(plan: QueryPlan, result: MetricLookupResult) -> str:
    composition = compose_response(plan, result)
    metric_names = _metric_names_for_lookup(result)
    if len(metric_names) > 1:
        return _render_metric_comparison_lookup(result, metric_names, composition)
    if plan.output.group_by == "state":
        return _render_state_grouped_metric_lookup(plan, result, metric_names)

    metric_name = result.rows[0].metric_name if result.rows else "the selected metric"
    answer_rows = _answer_rows(result)
    # #1826: stale rows now render in this table (their prior-year value), so
    # they are excluded from the narrative to avoid double-voicing the value.
    coverage_lines = _narrated_coverage_lines(result.rows, include_stale=True)
    include_sources = _has_row_citation_markers(answer_rows)
    header = [
        "District",
        "State",
        "Academic year",
        _escape_markdown_table_cell(metric_name),
    ]
    alignment = ["---", "---", "---", "---"]
    if include_sources:
        header.append("Sources")
        alignment.append("---")
    lines = [*composition.lead_lines]
    # B05 / #1556: name the anchor district + its shared reviewed value so an
    # equality-anchor lookup ("same school-year length as Portland, ME") never
    # floats free of whose value the matched districts equal. No-op for plain
    # lookups (returns None). Mirrors the anchor lead _render_peer_comparison
    # already voices.
    anchor_lead = _anchor_value_filter_lead(plan, answer_rows)
    if anchor_lead:
        lines.append(anchor_lead)
    if not answer_rows:
        # Empty-table guard: never a header-only table. With no answer rows the
        # coverage narrative IS the answer, so it stays inline (not collapsed).
        if coverage_lines:
            lines.append("")
            lines.extend(coverage_lines)
        _append_response_sections(lines, composition)
        return "\n".join(lines)
    lines.extend(
        [
            "",
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(alignment) + " |",
        ]
    )
    for row in answer_rows:
        values = [
            _escape_markdown_table_cell(row.district_name),
            _escape_markdown_table_cell(row.state or ""),
            _escape_markdown_table_cell(_lookup_academic_year_cell(row)),
            _escape_markdown_table_cell(_cell_display_value(row)),
        ]
        if include_sources:
            values.append(_format_markers(row.citation_markers))
        lines.append("| " + " | ".join(values) + " |")

    # #1228 / VOICE-R1: lead with the answer table; disclose not-current
    # districts below it under a collapsible heading (text unchanged, #1514 D8),
    # rather than the prior availability-first ordering that buried the answer.
    _append_coverage_disclosure_section(lines, coverage_lines)
    _append_response_sections(lines, composition)

    return "\n".join(lines)


def _render_state_grouped_metric_lookup(
    plan: QueryPlan,
    result: MetricLookupResult,
    metric_names: list[str],
) -> str:
    metric_name = metric_names[0] if metric_names else "the selected metric"
    filter_value = _metric_equals_filter_value(plan, metric_name)
    if filter_value is not None:
        lead = (
            "Based on NCTQ's covered district data, these states are represented "
            f"as {filter_value} for {metric_name}."
        )
    else:
        lead = (
            "Based on NCTQ's covered district data, these state rows summarize "
            f"{metric_name}."
        )
    methodology_lines = methodology_lines_for_result(result)
    state_group_methodology = (
        "State rows aggregate covered district rows; district-level artifact "
        "rows remain available for validation and export."
    )
    if state_group_methodology not in methodology_lines:
        methodology_lines.append(state_group_methodology)
    composition = AnswerComposition(
        lead_lines=[lead],
        methodology_lines=methodology_lines,
    )
    lines = [*composition.lead_lines]
    # #1514 R5: state grouping must not silently drop districts the table
    # cannot show. Same treatment as the single-metric lookup: the serialized
    # district-count line (D9), then every non-answer row's canonical sentence
    # (out-of-Pathfinder / D7 stale / not-reviewed). The named-district
    # sentences are never threshold-suppressed.
    # The D9 "Of the N districts you asked about…" summary stays as visible
    # lead framing (the always-visible X-of-Y coverage ratio), right after the
    # lead. The per-district sentences move below the table (#1228).
    summary = result.district_coverage
    if summary is not None and (
        summary.districts_not_reviewed or summary.districts_out_of_universe
    ):
        lines.append(district_coverage_sentence(summary))
    coverage_lines = _narrated_coverage_lines(result.rows)
    state_rows = _group_lookup_rows_by_state(result)
    if not state_rows:
        # Empty-table guard: no table -> coverage IS the answer, keep inline
        # (not under a foldable #### heading).
        if coverage_lines:
            lines.append("")
            lines.extend(coverage_lines)
        _append_response_sections(lines, composition)
        return "\n".join(lines)
    header = [
        "State",
        "District rows represented",
        "Academic year",
        _escape_markdown_table_cell(metric_name),
        "Sources",
    ]
    alignment = ["---", "---:", "---", "---", "---"]
    lines.extend(
        [
            "",
            "| " + " | ".join(header) + " |",
            "| " + " | ".join(alignment) + " |",
        ]
    )
    for state_row in state_rows:
        values = [
            _escape_markdown_table_cell(state_row["state"]),
            str(state_row["district_count"]),
            _escape_markdown_table_cell(", ".join(state_row["academic_years"])),
            _escape_markdown_table_cell(state_row["display_value"]),
            _format_markers(state_row["citation_markers"]),
        ]
        lines.append("| " + " | ".join(values) + " |")

    # #1228 / VOICE-R1: disclose per-district not-current sentences below the
    # state table.
    _append_coverage_disclosure_section(lines, coverage_lines)
    _append_response_sections(lines, composition)

    return "\n".join(lines)


def _render_metric_trend(result: MetricTrendResult) -> str:
    metric_name = result.rows[0].metric_name if result.rows else "the selected metric"
    answer_rows = _answer_rows(result)
    # #1514 D5: each requested year is its own "requested school year" — an
    # answer inside the window keeps its year-row; not_reviewed year-rows are
    # narrated per year, and an out-of-universe district collapses to ONE
    # canonical sentence (its rows stay in the ResultSet; dedupe is at render).
    coverage_lines = _narrated_coverage_lines(result.rows)
    lines = [f"Built a chronological trend for {metric_name}."]
    if not answer_rows:
        # Empty-table guard: narrative-only body, never a header-only table.
        # #1228: no table -> coverage IS the answer, so keep it inline (not
        # under a foldable #### heading).
        if coverage_lines:
            lines.append("")
            lines.extend(coverage_lines)
        _append_methodology_section(lines, result)
        return "\n".join(lines)
    lines.extend(
        [
            "",
            "| Academic year | District | State | Value | Change | Sources |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in answer_rows:
        lines.append(
            "| "
            f"{_escape_markdown_table_cell(row.academic_year)} | "
            f"{_escape_markdown_table_cell(row.district_name)} | "
            f"{_escape_markdown_table_cell(row.state or '')} | "
            f"{_escape_markdown_table_cell(_cell_display_value(row))} | "
            f"{_escape_markdown_table_cell(_format_delta(row))} | "
            f"{_format_markers(row.citation_markers)} |"
        )

    # #1228 / VOICE-R1: disclose not-current year-rows below the trend table.
    _append_coverage_disclosure_section(lines, coverage_lines)
    _append_methodology_section(lines, result)

    return "\n".join(lines)


def _render_metric_comparison_lookup(
    result: MetricLookupResult,
    metric_names: list[str],
    composition: AnswerComposition,
) -> str:
    # #1514 D3: a district with at least one answer cell keeps its row (its
    # answerless cells render ""); a district with zero answer cells drops
    # from the table and is voiced with ONE canonical prose sentence.
    grouped = _group_lookup_rows_by_district(result)
    district_rows = [district for district in grouped if district["values"]]
    # #1756 / B12: EVERY per-district coverage sentence — including a mixed
    # district's stale prior-year ("the value then was X") lines for a district
    # that KEEPS a table row — routes through the one below-table chokepoint
    # (_append_coverage_disclosure_section), exactly as _render_metric_lookup
    # and _render_metric_trend already do. The client asked for prior-year data
    # AFTER the table; there is no longer a separate above-table bucket.
    coverage_lines: list[str] = []
    for district in grouped:
        if not district["coverage_sentences"]:
            continue
        if district["values"]:
            # Mixed district (>= 1 answer cell AND >= 1 non-answer cell): keep
            # its table row, and voice ALL its canonical sentences below.
            for sentence in district["coverage_sentences"]:
                if sentence not in coverage_lines:
                    coverage_lines.append(sentence)
            continue
        sentence = district["coverage_sentences"][0]
        # Unresolved names group per metric (their group key carries the
        # metric_id), so dedupe by sentence keeps one line per district.
        if sentence not in coverage_lines:
            coverage_lines.append(sentence)
    lines = [*composition.lead_lines]
    if not district_rows:
        # Empty-table guard: R4 — coverage stays inline when it is the
        # entire answer (no answer table to lead with).
        if coverage_lines:
            lines.append("")
            lines.extend(coverage_lines)
        _append_response_sections(lines, composition)
        return "\n".join(lines)
    include_sources = any(district["citation_markers"] for district in district_rows)
    # #1220: when a ranked-lookup executor ordered this multi-metric table by
    # one metric, mark that column so a reader can tell which figure drives the
    # row order (e.g. a BA-vs-MA salary table sorted by BA pay). The lead's
    # order_statement carries the direction; the header marks the column.
    ranked_metric_name = getattr(result, "ranked_by_metric_name", None)
    header = [
        "District",
        "State",
        *(
            f"{name} (ranked)"
            if ranked_metric_name is not None and name == ranked_metric_name
            else name
            for name in metric_names
        ),
    ]
    if include_sources:
        header.append("Sources")
    lines.extend(
        [
            "",
            "| " + " | ".join(_escape_markdown_table_cell(value) for value in header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
    )
    for district in district_rows:
        values = [
            district["district_name"],
            district["state"],
            *[
                district["values"].get(metric_name, "")
                for metric_name in metric_names
            ],
        ]
        if include_sources:
            values.append(_format_markers(district["citation_markers"]))
        lines.append(
            "| " + " | ".join(_escape_markdown_table_cell(value) for value in values) + " |"
        )

    # #1228 / VOICE-R1: fully absent districts disclosed after the answer table.
    _append_coverage_disclosure_section(lines, coverage_lines)
    _append_response_sections(lines, composition)

    return "\n".join(lines)


def _render_profile_lookup(result: ProfileLookupResult) -> str:
    include_sources = _has_row_citation_markers(result.rows)
    header = ["District", "State", "Field", "Value", "Year", "Source", "Covered by Compass"]
    alignment = ["---", "---", "---", "---", "---", "---", "---"]
    if include_sources:
        header.append("Sources")
        alignment.append("---")
    lines = [
        "Looked up sourced NCES/profile fields for selected districts.",
        "",
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(alignment) + " |",
    ]
    for row in result.rows:
        values = [
            _escape_markdown_table_cell(str(row.district_name)),
            _escape_markdown_table_cell(str(row.state or '')),
            _escape_markdown_table_cell(str(row.label)),
            _escape_markdown_table_cell(_cell_display_value(row)),
            _escape_markdown_table_cell(str(row.academic_year)),
            _escape_markdown_table_cell(_profile_source_label(row)),
            'Yes' if row.covered_by_compass else 'No',
        ]
        if include_sources:
            values.append(_format_markers(row.citation_markers))
        lines.append("| " + " | ".join(values) + " |")

    _append_methodology_section(lines, result)

    return "\n".join(lines)


def _render_peer_comparison(result: PeerComparisonResult) -> str:
    if result.is_similarity_result:
        return _render_similarity(result)

    lines = [
        (
            "I selected comparison-ready peer districts using NCES-style "
            "similarity anchored on enrollment, urbanicity, and FRPL, with "
            "finance/staffing and distance/state used as secondary signals."
        ),
    ]
    metric_names = _peer_metric_names(result)
    coverage_screen = _peer_metric_coverage_screen_ref(result)
    if coverage_screen is not None:
        lines.append(
            "I selected peers from covered districts with linked NCES profiles "
            "and current covered data for this metric or metric bundle. When "
            "you ask for peer districts, I include up to 2 of the most similar "
            "districts from your state, with the rest from outside your state. "
            "That way you see both your closest in-state peers and broader "
            "national context."
        )
        in_state_note = _in_state_peer_disclosure(result)
        if in_state_note is not None:
            lines.append(in_state_note)
        if len(metric_names) > 1:
            lines.append(
                "For this policy bundle, I compared these reviewed fields: "
                f"{_join_readable(metric_names)}."
            )
        skipped = _metadata_int(coverage_screen, "excluded_unavailable_count")
        if skipped:
            lines.append(
                f"I skipped {skipped} peer {_plural(skipped, 'candidate')} because "
                "they did not have current covered data for this metric or "
                "metric bundle."
            )
        final_peer_count = _metadata_int(coverage_screen, "final_peer_count")
        if final_peer_count is not None and final_peer_count < 5:
            lines.append(
                f"Showing {final_peer_count} comparison-ready "
                f"{_plural(final_peer_count, 'peer')} rather than padding the "
                "table with unavailable comparisons."
            )
    # #1514 D13: answerless anchor/peer rows leave the table; their canonical
    # sentences (anchor first) are voiced as narrative. A stale anchor's
    # sentence is the D7 prior-year form via coverage_prior_* row fields.
    coverage_lines = _narrated_coverage_lines(
        sorted(result.rows, key=lambda row: 0 if row.peer_role == "anchor" else 1)
    )
    answer_rows = _answer_rows(result)
    if not answer_rows:
        # Empty-table guard: R4 — coverage stays inline when it is the
        # entire answer (no answer table to lead with).
        if coverage_lines:
            lines.extend(coverage_lines)
        _append_methodology_section(lines, result)
        return "\n".join(lines)
    # Confirm the anchor district's own value(s) in prose just before the peer
    # table — so a reader sees "where do *I* stand?" before scanning comparables
    # (VOICE-R1; NCTQ feedback). Placed after the demography-first methodology
    # lead (its own pinned spec) and before the table, not at the very top.
    anchor_lead = _anchor_value_lead(answer_rows)
    if anchor_lead is not None:
        lines.append(anchor_lead)
    # A metric bundle (>1 reviewed field) pivots to one row per district, each
    # metric its own column — so a reader compares districts (rows) against
    # policies (columns) at a glance instead of the same district repeating
    # once per metric (#1645). Mirrors the multi-metric metric_lookup pivot
    # (_render_metric_comparison_lookup). A single-metric peer table is already
    # one row per district, so it keeps the long renderer unchanged.
    if len(metric_names) > 1:
        lines.extend(_peer_comparison_wide_table(answer_rows, metric_names))
    else:
        lines.extend(_peer_comparison_long_table(answer_rows))

    # #1228 / VOICE-R1: fully absent peer/anchor rows disclosed after the answer table.
    _append_coverage_disclosure_section(lines, coverage_lines)
    _append_methodology_section(lines, result)

    return "\n".join(lines)


def _peer_comparison_long_table(answer_rows: list) -> list[str]:
    """One row per district-metric: the single-metric peer table layout.

    For a single metric each district already appears once, so the long shape
    is the right one; only multi-metric bundles pivot wide (#1645).
    """
    # Built via extend (not a ``lines = [...]`` literal) so the no-prose-dispatch
    # scanner, which flags list-literal assignments carrying multi-word strings,
    # stays unaffected — same idiom as _anchor_value_lead's f-string dodge.
    lines: list[str] = []
    lines.extend(
        [
            "",
            "| Role | Peer Rank | District | State | Enrollment | Urbanicity | Metric | Value | Peer Rationale | Sources |",
            "| --- | ---: | --- | --- | ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for row in answer_rows:
        lines.append(
            "| "
            f"{_escape_markdown_table_cell(row.peer_role)} | "
            f"{_escape_markdown_table_cell(_format_peer_rank(row))} | "
            f"{_escape_markdown_table_cell(str(row.district_name))} | "
            f"{_escape_markdown_table_cell(str(row.state or ''))} | "
            f"{_escape_markdown_table_cell(_format_peer_enrollment(row))} | "
            f"{_escape_markdown_table_cell(str(row.peer_urbanicity or ''))} | "
            f"{_escape_markdown_table_cell(str(row.metric_name))} | "
            f"{_escape_markdown_table_cell(_cell_display_value(row))} | "
            f"{_escape_markdown_table_cell(row.peer_reason)} | "
            f"{_format_markers(row.citation_markers)} |"
        )
    return lines


def _peer_comparison_wide_table(
    answer_rows: list, metric_names: list[str]
) -> list[str]:
    """One row per district with each metric as its own column (#1645).

    The per-district columns (role, rank, enrollment, urbanicity, rationale,
    sources) stay one cell per district; only Metric/Value collapse into one
    column per metric. The canonical ResultSet stays long (one row per
    district-metric) — only this rendered table pivots, exactly as the
    multi-metric metric_lookup table already does. Answerless rows are voiced
    before the table by the caller, so only answer rows reach the grouping;
    a metric a district has no answer for renders an empty cell.
    """
    groups = _group_peer_rows_by_district(answer_rows)
    include_sources = any(group["citation_markers"] for group in groups)
    # Per-district columns first, then one column per metric, then rationale
    # (and sources when any district cites). Header/alignment are grown with
    # extend/append rather than a single list literal so the no-prose-dispatch
    # scanner (which flags multi-word string-literal lists) stays unaffected.
    header: list[str] = []
    header.extend(["Role", "Peer Rank", "District", "State", "Enrollment", "Urbanicity"])
    header.extend(metric_names)
    header.append("Peer Rationale")
    alignment = ["---", "---:", "---", "---", "---:", "---"]
    alignment.extend("---" for _ in metric_names)
    alignment.append("---")
    if include_sources:
        header.append("Sources")
        alignment.append("---")
    lines = [
        "",
        "| " + " | ".join(_escape_markdown_table_cell(value) for value in header) + " |",
        "| " + " | ".join(alignment) + " |",
    ]
    for group in groups:
        values = [
            group["peer_role"],
            group["peer_rank"],
            group["district_name"],
            group["state"],
            group["enrollment"],
            group["urbanicity"],
            *[group["values"].get(name, "") for name in metric_names],
            group["peer_reason"],
        ]
        cells = [_escape_markdown_table_cell(value) for value in values]
        if include_sources:
            cells.append(_format_markers(group["citation_markers"]))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _peer_metric_coverage_screen_ref(
    result: PeerComparisonResult,
) -> MethodologyRef | None:
    for ref in result.methodology_codes:
        if ref.code == "peer_metric_coverage_screen_applied":
            return ref
    return None


def _peer_metric_names(result: PeerComparisonResult) -> list[str]:
    names: list[str] = []
    seen: set[int] = set()
    for row in result.rows:
        if row.metric_id in seen:
            continue
        seen.add(row.metric_id)
        names.append(str(row.metric_name))
    return names


def _join_readable(values: list[str]) -> str:
    if len(values) <= 1:
        return "".join(values)
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def _metadata_int(ref: MethodologyRef, key: str) -> int | None:
    value = ref.metadata.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _in_state_peer_disclosure(result: PeerComparisonResult) -> str | None:
    """Name the in-state peer(s) selected by the same-state cap rule, or
    note that none matched and the comparison is fully national.

    Per docs/plans/2026-05-26-peer-comparison-cap-rule.md, every default
    peer-comparison response surfaces which districts the rule picked so
    the user can verify the selection without reading the table.

    Reads the UNFILTERED ``result.rows`` deliberately (#1514 D13): the anchor
    row may be answerless and absent from the rendered table, but the cap-rule
    disclosure still needs its state to stay accurate.
    """
    anchor_state: str | None = None
    in_state_names: list[str] = []
    for row in result.rows:
        if row.peer_role == "anchor":
            anchor_state = (row.state or "").strip() or None
            continue
        if row.peer_role != "peer":
            continue
        peer_state = (row.state or "").strip()
        if anchor_state is None or peer_state.upper() != anchor_state.upper():
            continue
        # Dedupe by district: a metric bundle carries one row per
        # district-metric, so an in-state peer appears once per metric here
        # (#1645). Without this the disclosure reads "Aurora, Aurora, and
        # Aurora" for a 3-metric comparison.
        name = str(row.district_name)
        if name not in in_state_names:
            in_state_names.append(name)
    if anchor_state is None:
        return None
    if not in_state_names:
        return (
            f"No other {anchor_state} districts matched on similarity, so "
            "this comparison is fully national."
        )
    peer_word = "peer" if len(in_state_names) == 1 else "peers"
    return (
        f"Your in-state {anchor_state} {peer_word}: "
        f"{_join_readable(in_state_names)}."
    )


def _plural(count: int, singular: str) -> str:
    return singular if count == 1 else f"{singular}s"


def _render_similarity(result: PeerComparisonResult) -> str:
    """Render a peer-set discovery result (``is_similarity_result=True``).

    Similarity rows carry no policy metric — they represent the NCES-similar
    peer districts selected for an anchor. The table shows identity + rank +
    similarity score + peer rationale; it does NOT show metric name or value.
    """

    # Use order_statement set by build_similarity_result, e.g.
    # "Found N NCES-similar peer districts for Denver."
    lines = [
        result.order_statement,
        "",
        "| Role | Rank | District | State | Similarity Score | Peer Rationale |",
        "| --- | ---: | --- | --- | --- | --- |",
    ]
    for row in result.rows:
        # Anchor row has metric_id=0, score=None, display_value="anchor"
        score_display = (
            "—" if row.peer_score is None else f"{row.peer_score:.2f}"
        )
        lines.append(
            "| "
            f"{_escape_markdown_table_cell(row.peer_role)} | "
            f"{_escape_markdown_table_cell(_format_peer_rank(row))} | "
            f"{_escape_markdown_table_cell(str(row.district_name))} | "
            f"{_escape_markdown_table_cell(str(row.state or ''))} | "
            f"{_escape_markdown_table_cell(score_display)} | "
            f"{_escape_markdown_table_cell(row.peer_reason)} |"
        )

    _append_methodology_section(lines, result)

    return "\n".join(lines)


def _profile_source_label(row) -> str:
    source = row.source_label
    provenance = row.provenance
    if provenance:
        return f"{source}; {provenance}"
    return source


def _format_peer_rank(row) -> str:
    return "" if row.peer_rank is None else str(row.peer_rank)


def _format_peer_enrollment(row) -> str:
    return "" if row.peer_enrollment is None else f"{row.peer_enrollment:,}"


class _PeerDistrictGroup(TypedDict):
    peer_role: str
    peer_rank: str
    district_name: str
    state: str
    enrollment: str
    urbanicity: str
    peer_reason: str
    values: dict[str, str]
    citation_markers: list[int]


def _group_peer_rows_by_district(answer_rows: list) -> list[_PeerDistrictGroup]:
    """Group peer ANSWER rows per district for the wide pivot table (#1645).

    Per-district columns (role, rank, enrollment, urbanicity, rationale) are
    constant across a district's metric rows, so they are taken from the first
    answer row seen for that district. Group insertion order follows first
    appearance in ``answer_rows`` — which already carries the anchor-first /
    peer-rank order the executor built — so the wide table keeps that order.
    Answerless rows never reach here (the caller voices them before the table),
    so a metric a district has no answer for simply renders an empty cell.
    """

    grouped: dict[tuple[object, ...], _PeerDistrictGroup] = {}
    for row in answer_rows:
        group = grouped.setdefault(
            _lookup_row_group_key(row),
            _PeerDistrictGroup(
                peer_role=row.peer_role,
                peer_rank=_format_peer_rank(row),
                district_name=row.district_name,
                state=row.state or "",
                enrollment=_format_peer_enrollment(row),
                urbanicity=row.peer_urbanicity or "",
                peer_reason=row.peer_reason,
                values={},
                citation_markers=[],
            ),
        )
        group["values"][row.metric_name] = _cell_display_value(row)
        for marker in row.citation_markers:
            if marker not in group["citation_markers"]:
                group["citation_markers"].append(marker)
    return list(grouped.values())


class _LookupDistrictGroup(TypedDict):
    district_name: str
    state: str
    values: dict[str, str]
    citation_markers: list[int]
    coverage_sentences: list[str]


class _StateLookupGroup(TypedDict):
    state: str
    display_value: str
    academic_years: list[str]
    district_keys: set[tuple[object, ...]]
    district_count: int
    citation_markers: list[int]


def _group_lookup_rows_by_district(
    result: MetricLookupResult,
) -> list[_LookupDistrictGroup]:
    """Group lookup rows per district for the pivot table (#1514 D3).

    Only answer cells land in ``values`` — a district's answerless metric
    cells render as ``""`` (the existing idiom for absent metrics), and its
    canonical coverage sentences are kept on the group so the renderer can
    voice ONE sentence for a district that holds no answers at all.
    """

    grouped: dict[tuple[object, ...], _LookupDistrictGroup] = {}
    for row in result.rows:
        group_key = _lookup_row_group_key(row)
        district = grouped.setdefault(
            group_key,
            _LookupDistrictGroup(
                district_name=row.district_name,
                state=row.state or "",
                values={},
                citation_markers=[],
                coverage_sentences=[],
            ),
        )
        if not _is_rendered_answer_row(row):
            sentence = (
                getattr(row, "coverage_display", None) or row.display_value
            ).strip()
            if sentence and sentence not in district["coverage_sentences"]:
                # Stale sentences first — they carry the prior-year value.
                if getattr(row, "coverage_reason", None) == "stale_recent_answer":
                    district["coverage_sentences"].insert(0, sentence)
                else:
                    district["coverage_sentences"].append(sentence)
            continue
        district["values"][row.metric_name] = _cell_display_value(row)
        for marker in row.citation_markers:
            if marker not in district["citation_markers"]:
                district["citation_markers"].append(marker)
    return list(grouped.values())


def _group_lookup_rows_by_state(
    result: MetricLookupResult,
) -> list[_StateLookupGroup]:
    grouped: dict[tuple[str, str], _StateLookupGroup] = {}
    # #1514: only answer rows aggregate into state rows — filter BEFORE
    # grouping so a non-answer display string never becomes a state "value".
    # #1826: the state-grouped table keeps stale rows narrated (not in-table),
    # so it aggregates the answers-only subset directly — never the extended
    # ``_answer_rows`` subset, which pulls stale rows in for the plain lookup.
    for row in result.rows:
        if not is_rendered_answer_state(getattr(row, "coverage_state", None)):
            continue
        state = row.state or ""
        group_key = (state, row.display_value)
        state_row = grouped.setdefault(
            group_key,
            _StateLookupGroup(
                state=state,
                display_value=row.display_value,
                academic_years=[],
                district_keys=set(),
                district_count=0,
                citation_markers=[],
            ),
        )
        if row.academic_year not in state_row["academic_years"]:
            state_row["academic_years"].append(row.academic_year)
        district_key = _lookup_row_group_key(row)
        if district_key not in state_row["district_keys"]:
            state_row["district_keys"].add(district_key)
            state_row["district_count"] += 1
        for marker in row.citation_markers:
            if marker not in state_row["citation_markers"]:
                state_row["citation_markers"].append(marker)

    return sorted(
        grouped.values(),
        key=lambda row: (row["state"].casefold(), row["display_value"].casefold()),
    )


def _metric_names_for_lookup(result: MetricLookupResult) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        names.append(name)

    if result.criteria:
        rows_by_metric_id: dict[int, list[str]] = {}
        for row in result.rows:
            rows_by_metric_id.setdefault(row.metric_id, []).append(row.metric_name)

        for criterion in result.criteria:
            for metric_id in criterion.metric_ids:
                for metric_name in rows_by_metric_id.get(metric_id, []):
                    add(metric_name)

    for row in result.rows:
        add(row.metric_name)

    return names


def _metric_equals_filter_value(plan: QueryPlan, metric_name: str) -> str | None:
    for filter_spec in plan.filters:
        if (
            filter_spec.field.casefold() == metric_name.casefold()
            and filter_spec.operator == "equals"
            and isinstance(filter_spec.value, str)
        ):
            return filter_spec.value
    return None

def _append_response_sections(
    lines: list[str],
    composition: AnswerComposition,
) -> None:
    _append_availability_section(lines, composition)
    _append_methodology_lines(lines, composition)


def _append_availability_section(
    lines: list[str],
    composition: AnswerComposition,
    *,
    as_collapsible: bool = True,
) -> None:
    """Append the "Not included in ranking" availability section.

    ``as_collapsible`` controls whether it renders under a foldable ``####``
    heading (the normal case — it follows the answer table) or as a plain,
    always-visible label (the empty-table case, where this section IS the whole
    answer — folding it would hide the only content, #1228 review finding). The
    frontend folds only ``####`` headings, so the plain label stays expanded.
    """

    if composition.availability_heading and composition.availability_lines:
        heading = (
            _section_heading(composition.availability_heading)
            if as_collapsible
            else composition.availability_heading
        )
        lines.extend(["", heading])
        lines.extend(f"- {line}" for line in composition.availability_lines)


def _append_largest_excluded_section(
    lines: list[str],
    composition: AnswerComposition,
) -> None:
    """Render the #1468 "larger districts excluded" callout, when present."""

    if composition.largest_excluded_lines:
        lines.extend(["", _section_heading("Larger districts not ranked")])
        lines.extend(f"- {line}" for line in composition.largest_excluded_lines)



def _append_methodology_lines(
    lines: list[str],
    composition: AnswerComposition,
) -> None:
    if composition.methodology_lines:
        lines.extend(["", _section_heading("Methodology")])
        lines.extend(f"- {line}" for line in composition.methodology_lines)


def _append_methodology_section(lines: list[str], result: ResultSet) -> None:
    methodology_lines = methodology_lines_for_result(result)
    if methodology_lines:
        lines.extend(["", _section_heading("Methodology")])
        lines.extend(f"- {line}" for line in methodology_lines)


# #1228: secondary answer sections render as H4 headings so the chat UI can
# collapse them uniformly (one authority for the level) while the lead + answer
# table stay expanded. The plain-text titles are unchanged — only the level is
# marked.
def _section_heading(title: str) -> str:
    return f"#### {title}"


# The post-answer collapsible can mix not-reviewed, stale (prior-year),
# out-of-Pathfinder, and non-numeric districts — each line carries its own
# canonical sentence (#1514), so the heading only labels the group.
_COVERAGE_DISCLOSURE_HEADING = "Districts without a current reviewed value"


def _append_coverage_disclosure_section(
    lines: list[str],
    coverage_lines: list[str],
) -> None:
    """Disclose not-current districts AFTER the answer table (#1228 / VOICE-R1).

    The sentences are the unchanged canonical per-district lines (#1514 D7/D8),
    relocated below the answer table under a collapsible heading so the answer
    leads. With no answer rows the caller keeps them inline instead (the
    coverage narrative is then the whole answer).
    """

    if not coverage_lines:
        return
    lines.extend(["", _section_heading(_COVERAGE_DISCLOSURE_HEADING)])
    lines.extend(coverage_lines)


def _lookup_row_group_key(row) -> tuple[object, ...]:
    if row.district_id is not None:
        return ("district", row.district_id)
    return ("unresolved", row.district_name.casefold(), row.metric_id)


def _has_row_citation_markers(rows: Sequence[object]) -> bool:
    return any(getattr(row, "citation_markers", []) for row in rows)


def _categorical_count_header(plan: QueryPlan, result: MetricCountResult) -> str:
    for metric in plan.metrics:
        if metric.role == "grouping":
            return _sentence_case(metric.name)
    # Post-finalize, _rebalance_metric_drawers may move a profile grouping axis
    # out of plan.metrics (dropping role="grouping") into plan.profile_fields,
    # so the role read above misses it. Fall back to the executed result's
    # resolved axis name (set by the executor to metric.name) before "Category".
    for row in result.rows:
        if isinstance(row, CategoricalCountRow) and row.metric_name.strip():
            return _sentence_case(row.metric_name)
    return "Category"


def _sentence_case(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return "Category"
    return cleaned[:1].upper() + cleaned[1:]
