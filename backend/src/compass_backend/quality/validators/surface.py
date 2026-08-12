"""Surface-consistency diagnostics — cross-surface parity for one ResultSet.

Surface Consistency is a structural diagnostic, not a top-level Compass
scorecard dimension. The seven-dimension scorecard measures answer accuracy;
these checks measure whether every rendered surface — markdown table, chart
JSON, CSV export, SSE data block, narrative prose, citations panel — derives
from the same canonical `ResultSet`. The user should see the same number in
every place it appears.

This module provides three deterministic checks:

- `_validate_markdown_table_parity`: parses the markdown table from the
  rendered body and asserts each visible cell equals the expected
  surface-rendered value derived from the corresponding answers-only
  `ResultSet.rows` entry (#1514: tables hold answer rows only; non-answer
  rows are voiced as narrative sentences). Uses the SAME formatters
  (`rendering/formatting.py`) the writer uses, so a change in escape/format
  rules in the writer propagates to both surfaces atomically.

- `_validate_csv_row_parity`: for each data row in `result.csv_export.rows`,
  asserts every column value matches the corresponding answers-only
  `result.rows` entry (#1514 D2: CSV data rows mirror the table). Doesn't
  catch escape drift (CSV has no escape) but catches projection/copy
  mistakes in `_default_csv_export_for_result`.

- `_validate_artifact_id_parity`: re-computes `compute_artifact_id(result)`
  and compares to the stamp on `manifest.metadata['artifact_id']`. A
  mismatch means the manifest carried an id from a different ResultSet
  than the one we're validating — i.e. something mutated between
  construction and render-emit.

All three are deterministic post-hoc checks; no LLM involvement.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from compass_backend.artifacts import (
    is_rendered_answer_state,
    is_stale_prior_value_row,
    lookup_renders_stale_prior_values,
    short_coverage_label,
)
from compass_backend.artifacts.identity import compute_artifact_id
from compass_backend.contracts.planning import QueryPlan
from compass_backend.contracts.validation import ValidationFinding
from compass_backend.rendering.formatting import (
    escape_markdown_cell,
    format_citation_markers,
)


def _expected_cell_display_value(row) -> str:
    """Mirror rendering/writer._cell_display_value so the validator's expected
    cell matches the renderer's emitted cell for non-covered rows (incl. the
    #1826 stale prior-value swap)."""

    if is_stale_prior_value_row(row):
        return row.coverage_prior_display_value or row.display_value
    coverage_state = getattr(row, "coverage_state", None)
    if coverage_state is None or coverage_state == "covered":
        return row.display_value
    return short_coverage_label(getattr(row, "coverage_reason", None), row.display_value)


def _is_rendered_answer_row(row) -> bool:
    """Mirror rendering/writer._is_rendered_answer_row (#1514).

    Tables and CSV data rows hold answer rows only (value, INA, or N/A);
    not_reviewed and out_of_universe rows are voiced as narrative sentences.
    Delegates to the shared ``is_rendered_answer_state`` predicate
    (artifacts/coverage.py) — the one None-as-answer row-partition authority
    the writer, chart, and CSV builders all use; never a local restatement.
    """

    return is_rendered_answer_state(getattr(row, "coverage_state", None))


def _expected_answer_rows(result: "ResultSet") -> list:
    """The subset of ``result.rows`` (order preserved) the renderer's tables and
    the CSV builder's data rows derive from.

    Mirrors rendering/writer._answer_rows: answer rows always, plus #1826 stale
    prior-value rows on the single-metric lookup surface (keyed on the same
    shared ``lookup_renders_stale_prior_values`` gate so the parity subset tracks
    the builders exactly)."""

    include_stale = lookup_renders_stale_prior_values(result)
    return [
        row
        for row in result.rows
        if _is_rendered_answer_row(row)
        or (include_stale and is_stale_prior_value_row(row))
    ]

from .._validation_helpers import _finding

if TYPE_CHECKING:
    from compass_backend.artifacts.results import ResultSet


# ─── Markdown table parser ───────────────────────────────────────────────────


# A markdown-table line: starts with `|`, ends with `|` (after stripping
# trailing whitespace). The separator line below the header consists of `|`,
# `-`, `:`, and whitespace only. The renderer always emits a single table per
# result type; the parser captures the first contiguous block.
_TABLE_LINE_RE = re.compile(r"^\|.*\|\s*$")
_SEPARATOR_LINE_RE = re.compile(r"^\|[\s:|-]*\|\s*$")


class _ParsedTable:
    """One markdown table extracted from a rendered body.

    `headers` is the list of column titles in order. `rows` is a list of
    dicts keyed by header name. Both preserve the renderer's escape form
    (e.g. `\\|` for embedded pipes) — the validator un-escapes when
    comparing to source row values.
    """

    __slots__ = ("headers", "rows")

    def __init__(self, headers: list[str], rows: list[dict[str, str]]) -> None:
        self.headers = headers
        self.rows = rows


def _extract_first_markdown_table(body: str) -> _ParsedTable | None:
    """Locate and parse the first markdown table in `body`, or return None.

    The renderer guarantees: at most one data table per response, separator
    line directly below header, all body rows have the same column count.
    The parser is permissive about leading/trailing whitespace per line so
    it survives small writer changes (e.g. extra blank line before the table).
    """
    lines = body.splitlines()
    table_lines: list[str] = []
    capturing = False
    for line in lines:
        if _TABLE_LINE_RE.match(line.rstrip()):
            table_lines.append(line.rstrip())
            capturing = True
        elif capturing:
            # Run of table lines ended.
            break
    if len(table_lines) < 3:
        # Need at least: header | separator | one body row.
        return None
    if not _SEPARATOR_LINE_RE.match(table_lines[1]):
        return None
    headers = _split_table_row(table_lines[0])
    body_rows: list[dict[str, str]] = []
    for line in table_lines[2:]:
        cells = _split_table_row(line)
        if len(cells) != len(headers):
            # Malformed row; bail rather than mis-aligning columns.
            return None
        body_rows.append(dict(zip(headers, cells, strict=True)))
    return _ParsedTable(headers=headers, rows=body_rows)


# Sentinel for escaped pipes (`\|`) so we can split on `|` cleanly. The
# byte sequence is rare enough to be safe; replaced both directions.
_ESCAPED_PIPE = "\\|"
_ESCAPED_PIPE_PLACEHOLDER = "\x00ESCAPED_PIPE\x00"


def _split_table_row(line: str) -> list[str]:
    """Split a markdown table row on unescaped `|`.

    The renderer escapes embedded pipes as `\\|` (see
    `rendering/formatting.escape_markdown_cell`). The parser preserves that
    escape in the returned cell — the validator un-escapes when comparing
    to source row values. Leading/trailing `|` are dropped (they're the
    table border, not cell delimiters).
    """
    safe = line.replace(_ESCAPED_PIPE, _ESCAPED_PIPE_PLACEHOLDER)
    parts = safe.split("|")
    # Drop empty leading/trailing parts from the surrounding `|` border.
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.strip().replace(_ESCAPED_PIPE_PLACEHOLDER, _ESCAPED_PIPE) for p in parts]


# ─── Per-result-type column → expected-value mappers ─────────────────────────
#
# Each mapper takes one source row and a column header name, and returns the
# expected rendered cell. Returns None if the column doesn't apply to this
# row type — used to skip checking columns whose mapping isn't known yet
# (e.g. metric-name columns whose exact text isn't fixed at parse time).


def _expected_ranking_cell(row, column: str) -> str | None:
    """Expected markdown cell for a `RankingRow` column."""
    if column == "Rank":
        return str(row.rank)
    if column == "District":
        return escape_markdown_cell(row.district_name)
    if column == "State":
        return escape_markdown_cell(row.state or "")
    if column == "Sources":
        return format_citation_markers(row.citation_markers)
    # The renderer headers the sort column with the *label* (the human title,
    # e.g. "FRPL %"), preferring sort_metric_label over sort_metric_name — see
    # rendering.shared.sort_metric_name. On a PROFILE-FIELD sort, sort_metric_name
    # carries the machine field_key ("frpl_pct"), which never equals the rendered
    # header, so matching it here let the FRPL column fall through to the metric
    # branch below and compare against the salary display_value — a false
    # markdown_cell_drift on every "rank by profile field, show the metric" answer
    # (#1721). Match the rendered header (label-then-name), suffix-stripped.
    column_plain = _strip_ranked_suffix(column)
    sort_header = getattr(row, "sort_metric_label", None) or getattr(
        row, "sort_metric_name", None
    )
    if sort_header and column_plain == _strip_ranked_suffix(sort_header):
        return escape_markdown_cell(str(getattr(row, "sort_display_value", "") or ""))
    metric_name = getattr(row, "metric_name", None)
    if metric_name and column_plain == metric_name:
        return escape_markdown_cell(_expected_cell_display_value(row))
    # Metric-name column is dynamic ("Starting salary", etc.). Match by
    # process of elimination: any column not listed above with a matching
    # `display_value` is the metric column.
    if column not in {"Rank", "District", "State", "Sources"}:
        return escape_markdown_cell(_expected_cell_display_value(row))
    return None


def _expected_lookup_cell(row, column: str) -> str | None:
    """Expected markdown cell for a `MetricValueRow` (lookup) column.

    Only validates "stable" columns whose mapping is unambiguous regardless
    of whether the table is single-metric or multi-metric (pivot). Skips
    metric-name columns because in a pivot table they're attached to a
    different `result.rows` entry — comparing `row.display_value` would
    misfire. Phase B can add per-(district, metric) cell matching for
    pivot tables; for MVP, catching district / state / citation drift on
    the stable columns is the load-bearing check.
    """
    if column == "District":
        return escape_markdown_cell(row.district_name)
    if column == "State":
        return escape_markdown_cell(row.state or "")
    if column == "Academic year":
        # #1826: a stale row's Academic-year cell shows the PRIOR year it was
        # last reviewed (mirroring writer._lookup_academic_year_cell).
        if is_stale_prior_value_row(row):
            return escape_markdown_cell(row.coverage_prior_academic_year or row.academic_year)
        return escape_markdown_cell(row.academic_year)
    if column == "Sources":
        return format_citation_markers(row.citation_markers)
    return None


def _lookup_metric_names(result: "ResultSet") -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        names.append(name)

    criteria = getattr(result, "criteria", [])
    if criteria:
        rows_by_metric_id: dict[int, list[str]] = {}
        for row in result.rows:
            rows_by_metric_id.setdefault(row.metric_id, []).append(row.metric_name)
        for criterion in criteria:
            for metric_id in criterion.metric_ids:
                for metric_name in rows_by_metric_id.get(metric_id, []):
                    add(metric_name)

    for row in result.rows:
        add(row.metric_name)
    return names


def _lookup_row_group_key(row) -> tuple[object, ...]:
    if row.district_id is not None:
        return ("district", row.district_id)
    return ("unresolved", row.district_name.casefold(), row.metric_id)


def _expected_lookup_pivot_groups(result: "ResultSet") -> list[dict[str, object]]:
    """Expected pivot-table rows, mirroring writer._group_lookup_rows_by_district.

    #1514 D3: only answer cells land in ``values`` — a district with at least
    one answer cell keeps its row (answerless metric cells render ``""``); a
    district with zero answer cells drops from the table entirely (it is
    voiced as one canonical prose sentence instead). Group insertion order
    follows first appearance in ``result.rows``, matching the renderer.
    """
    grouped: dict[tuple[object, ...], dict[str, object]] = {}
    for row in result.rows:
        group = grouped.setdefault(
            _lookup_row_group_key(row),
            {
                "district_name": row.district_name,
                "state": row.state or "",
                "values": {},
                "citation_markers": [],
            },
        )
        if not _is_rendered_answer_row(row):
            continue
        group["values"][row.metric_name] = _expected_cell_display_value(row)
        citation_markers = group["citation_markers"]
        for marker in row.citation_markers:
            if marker not in citation_markers:
                citation_markers.append(marker)
    return [group for group in grouped.values() if group["values"]]


def _peer_metric_names(result: "ResultSet") -> list[str]:
    """Bundle metric names in first-appearance order, mirroring
    writer._peer_metric_names — the wide peer table's column order."""
    names: list[str] = []
    seen: set[int] = set()
    for row in result.rows:
        if row.metric_id in seen:
            continue
        seen.add(row.metric_id)
        names.append(str(row.metric_name))
    return names


def _expected_peer_pivot_groups(result: "ResultSet") -> list[dict[str, object]]:
    """Expected wide peer-table rows, mirroring writer._group_peer_rows_by_district.

    One row per district; per-district columns (role, rank, enrollment,
    urbanicity, rationale) are constant across a district's metric rows.
    Non-answer rows are skipped BEFORE the group is created, mirroring the
    renderer's ``_group_peer_rows_by_district`` (which only ever sees answer
    rows): per-district fields are taken from the first ANSWER row, and a
    district with zero answer cells never forms a group (it is voiced as prose
    before the table). Group insertion order follows first-answer-row
    appearance — districts are contiguous and anchor-first — so it matches the
    renderer's grouping order exactly. A metric a district has no answer for
    simply renders an empty cell (``values.get(name, "")``).
    """
    grouped: dict[tuple[object, ...], dict[str, object]] = {}
    for row in result.rows:
        if not _is_rendered_answer_row(row):
            continue
        group = grouped.setdefault(
            _lookup_row_group_key(row),
            {
                "peer_role": row.peer_role,
                "peer_rank": "" if row.peer_rank is None else str(row.peer_rank),
                "district_name": row.district_name,
                "state": row.state or "",
                "enrollment": (
                    "" if row.peer_enrollment is None else f"{row.peer_enrollment:,}"
                ),
                "urbanicity": row.peer_urbanicity or "",
                "peer_reason": row.peer_reason,
                "values": {},
                "citation_markers": [],
            },
        )
        group["values"][row.metric_name] = _expected_cell_display_value(row)
        citation_markers = group["citation_markers"]
        for marker in row.citation_markers:
            if marker not in citation_markers:
                citation_markers.append(marker)
    return list(grouped.values())


_RANKED_SUFFIX = " (ranked)"


def _strip_ranked_suffix(header: str) -> str:
    """Return a rendered column header with the #1220 ``(ranked)`` marker removed.

    The renderer marks the sorted metric column header ``"{name} (ranked)"`` so
    readers can see which figure drives row order (writer.py, #1220). The parity
    validator compares against the plain ``metric_name`` the ResultSet carries,
    so it must normalise the rendered header — both for the column-membership
    check and the per-cell value lookup — or it raises a false-positive
    ``markdown_column_missing`` / ``markdown_cell_drift`` that swallows a correct
    table (case 1006).
    """
    if header.endswith(_RANKED_SUFFIX):
        return header[: -len(_RANKED_SUFFIX)]
    return header


def _validate_lookup_pivot_table_parity(
    result: "ResultSet",
    table: _ParsedTable,
) -> list[ValidationFinding]:
    metric_names = _lookup_metric_names(result)
    expected_groups = _expected_lookup_pivot_groups(result)
    required_columns = ["District", "State", *metric_names]
    # Mirror the writer: the Sources column exists when any RENDERED district
    # row carries markers (answer-row markers of kept groups), not when any
    # raw artifact row does — non-answer rows never render markers (#1514).
    include_sources = any(group["citation_markers"] for group in expected_groups)
    if include_sources:
        required_columns.append("Sources")

    # #1220 adds a " (ranked)" suffix to the sorted column header so readers
    # can see which metric drives row order.  Normalise before membership check
    # so a plain metric name matches its suffixed rendered header and does not
    # produce a false-positive markdown_column_missing finding.
    normalised_headers = {_strip_ranked_suffix(h) for h in table.headers}
    for column in required_columns:
        if column not in normalised_headers:
            return [
                _finding(
                    code="markdown_column_missing",
                    dimension="surface_consistency",
                    message=f"Markdown pivot table is missing column {column!r}.",
                    metadata={"column": column, "headers": table.headers},
                )
            ]

    if len(table.rows) != len(expected_groups):
        return [
            _finding(
                code="markdown_row_count_mismatch",
                dimension="surface_consistency",
                message=(
                    "Markdown pivot table row count does not match grouped "
                    "ResultSet lookup rows."
                ),
                metadata={
                    "rendered_row_count": len(table.rows),
                    "expected_row_count": len(expected_groups),
                },
            )
        ]

    for row_index, (parsed_cells, expected_group) in enumerate(
        zip(table.rows, expected_groups, strict=True)
    ):
        expected_cells: dict[str, str] = {
            "District": escape_markdown_cell(str(expected_group["district_name"])),
            "State": escape_markdown_cell(str(expected_group["state"])),
        }
        values = expected_group["values"]
        for metric_name in metric_names:
            expected_cells[metric_name] = escape_markdown_cell(
                str(values.get(metric_name, ""))
            )
        if include_sources:
            expected_cells["Sources"] = format_citation_markers(
                expected_group["citation_markers"]
            )

        # Key the parsed cells by their normalised header so a ranked metric
        # column (rendered "{metric} (ranked)") matches the plain metric_name in
        # expected_cells — otherwise the lookup returns None and a correct value
        # reads as markdown_cell_drift (case 1006).
        normalised_cells = {
            _strip_ranked_suffix(key): value for key, value in parsed_cells.items()
        }
        for column, expected in expected_cells.items():
            rendered = normalised_cells.get(column)
            if rendered != expected:
                return [
                    _finding(
                        code="markdown_cell_drift",
                        dimension="surface_consistency",
                        message=(
                            f"Markdown table row {row_index} column "
                            f"{column!r}: rendered cell does not match "
                            "the grouped ResultSet lookup value."
                        ),
                        metadata={
                            "row_index": row_index,
                            "column": column,
                            "rendered_cell": rendered,
                            "expected_cell": expected,
                        },
                    )
                ]
    return []


def _validate_peer_pivot_table_parity(
    result: "ResultSet",
    table: _ParsedTable,
) -> list[ValidationFinding]:
    """Assert the wide peer-comparison table is one row per district (#1645).

    Mirrors ``_validate_lookup_pivot_table_parity`` for the peer table: the
    ``markdown_row_count_mismatch`` finding here is the shape guard — it fails
    if the rendered table did not pivot to one data row per answer-district.
    Per-district and per-metric cells are checked against the grouped
    ResultSet using the same formatters the writer uses.
    """
    metric_names = _peer_metric_names(result)
    expected_groups = _expected_peer_pivot_groups(result)
    required_columns = [
        "Role",
        "Peer Rank",
        "District",
        "State",
        "Enrollment",
        "Urbanicity",
        *metric_names,
        "Peer Rationale",
    ]
    include_sources = any(group["citation_markers"] for group in expected_groups)
    if include_sources:
        required_columns.append("Sources")

    for column in required_columns:
        if column not in table.headers:
            return [
                _finding(
                    code="markdown_column_missing",
                    dimension="surface_consistency",
                    message=f"Markdown peer pivot table is missing column {column!r}.",
                    metadata={"column": column, "headers": table.headers},
                )
            ]

    if len(table.rows) != len(expected_groups):
        return [
            _finding(
                code="markdown_row_count_mismatch",
                dimension="surface_consistency",
                message=(
                    "Markdown peer pivot table row count does not match the "
                    "one-row-per-district grouped ResultSet."
                ),
                metadata={
                    "rendered_row_count": len(table.rows),
                    "expected_row_count": len(expected_groups),
                },
            )
        ]

    for row_index, (parsed_cells, expected_group) in enumerate(
        zip(table.rows, expected_groups, strict=True)
    ):
        expected_cells: dict[str, str] = {
            "Role": escape_markdown_cell(str(expected_group["peer_role"])),
            "Peer Rank": escape_markdown_cell(str(expected_group["peer_rank"])),
            "District": escape_markdown_cell(str(expected_group["district_name"])),
            "State": escape_markdown_cell(str(expected_group["state"])),
            "Enrollment": escape_markdown_cell(str(expected_group["enrollment"])),
            "Urbanicity": escape_markdown_cell(str(expected_group["urbanicity"])),
            "Peer Rationale": escape_markdown_cell(str(expected_group["peer_reason"])),
        }
        values = expected_group["values"]
        for metric_name in metric_names:
            expected_cells[metric_name] = escape_markdown_cell(
                str(values.get(metric_name, ""))
            )
        if include_sources:
            expected_cells["Sources"] = format_citation_markers(
                expected_group["citation_markers"]
            )

        for column, expected in expected_cells.items():
            rendered = parsed_cells.get(column)
            if rendered != expected:
                return [
                    _finding(
                        code="markdown_cell_drift",
                        dimension="surface_consistency",
                        message=(
                            f"Markdown peer pivot table row {row_index} column "
                            f"{column!r}: rendered cell does not match the "
                            "grouped ResultSet peer value."
                        ),
                        metadata={
                            "row_index": row_index,
                            "column": column,
                            "rendered_cell": rendered,
                            "expected_cell": expected,
                        },
                    )
                ]
    return []


def _table_absent_findings(*, expected_row_count: int) -> list[ValidationFinding]:
    """Findings for a rendered body that contains NO markdown table.

    Legitimate when zero answer rows are expected — the renderer's #1514
    empty-table guard emits a narrative-only body (never a header-only
    table). With answer rows expected, a missing table means the table was
    dropped somewhere between writer and body: fail loudly rather than
    silently disabling the parity check.
    """

    if expected_row_count == 0:
        return []
    return [
        _finding(
            code="markdown_row_count_mismatch",
            dimension="surface_consistency",
            message=(
                "Rendered body contains no markdown table but the "
                "answers-only subset of ResultSet.rows is non-empty."
            ),
            metadata={
                "rendered_row_count": 0,
                "expected_row_count": expected_row_count,
                "table_absent": True,
            },
        )
    ]


# ─── Public L1 validators ────────────────────────────────────────────────────


def _validate_markdown_table_parity(
    plan: QueryPlan,
    result: "ResultSet",
    rendered_body: str | None,
) -> list[ValidationFinding]:
    """Assert each markdown table cell matches the expected formatter output.

    Only runs when `rendered_body` is present (skipped on pre-render L1) and
    `result.rows` is non-empty. For mapped result types, a body with NO
    markdown table is legitimate only when the answers-only expected row set
    is empty (the renderer's #1514 empty-table guard emits a narrative-only
    body); with answer rows expected, a missing table fails loudly as
    `markdown_row_count_mismatch` — silently passing here was the hole that
    let a dropped table go unvalidated.

    Per-result-type cell expectations live in `_expected_<type>_cell`. New
    result types should add their own mapper; missing mappers cause the
    validator to skip silently (better than a noisy false positive).
    """
    # Lazy import to avoid a circular import with quality/validation.py.
    from compass_backend.artifacts.results import (
        MetricLookupResult,
        MetricRankingResult,
        PeerComparisonResult,
    )

    if rendered_body is None or not result.rows:
        return []

    if isinstance(result, MetricRankingResult):
        expected_cell = _expected_ranking_cell
    elif isinstance(result, MetricLookupResult):
        if len(_lookup_metric_names(result)) > 1:
            table = _extract_first_markdown_table(rendered_body)
            if table is None:
                # Pivot tables drop only districts with zero answer cells
                # (D3); a table-less body is legitimate exactly when every
                # district is answerless.
                return _table_absent_findings(
                    expected_row_count=len(_expected_lookup_pivot_groups(result)),
                )
            return _validate_lookup_pivot_table_parity(result, table)
        if plan.output.group_by == "state":
            # State-grouped lookup tables aggregate source rows under a
            # different contract; they stay out of this row-by-row validator.
            return []
        expected_cell = _expected_lookup_cell
    elif isinstance(result, PeerComparisonResult):
        # A multi-metric peer comparison pivots to one row per district (#1645),
        # exactly like the multi-metric lookup table above. The single-metric
        # peer table is already one row per district but has no cell mapper yet,
        # so it stays exempt (skips silently, as it did before). The similarity
        # table carries no metric columns and is out of scope.
        if getattr(result, "is_similarity_result", False):
            return []
        if len(_peer_metric_names(result)) > 1:
            table = _extract_first_markdown_table(rendered_body)
            if table is None:
                return _table_absent_findings(
                    expected_row_count=len(_expected_peer_pivot_groups(result)),
                )
            return _validate_peer_pivot_table_parity(result, table)
        return []
    else:
        # Result types without a cell mapper stay exempt and skip silently:
        # count and similarity render aggregate (non-row) tables, and
        # profile / trend / composite have no mapper yet — a table-less body
        # is legitimate for all of them.
        return []

    # #1514: the table holds the answers-only subset of result.rows — non-answer
    # rows (not_reviewed, out_of_universe) are voiced as narrative sentences and
    # never render as data rows. The expected list anchors to that subset.
    expected_rows = _expected_answer_rows(result)
    # Mirror the renderer's preview truncation (writer._display_rows): a
    # ranking over the display limit legitimately shows only the first
    # display_limit answer rows, with a "Showing X of N" notice.
    if isinstance(result, MetricRankingResult):
        # Lazy import: rendering.writer is import-heavy and quality must not
        # restate the display-limit rule (one authority).
        from compass_backend.rendering.writer import _ranking_display_limit

        display_limit = _ranking_display_limit(plan, result)
        if display_limit is not None:
            expected_rows = expected_rows[:display_limit]

    table = _extract_first_markdown_table(rendered_body)
    if table is None:
        return _table_absent_findings(expected_row_count=len(expected_rows))

    # Parity must fail loudly on a row-count mismatch, not disable itself.
    # (Pre-#1514 this guard compared against len(result.rows) and silently
    # skipped — which, once non-answer rows stopped rendering, would have
    # turned the whole check off for exactly the answers it should cover.)
    if len(table.rows) != len(expected_rows):
        return [
            _finding(
                code="markdown_row_count_mismatch",
                dimension="surface_consistency",
                message=(
                    "Markdown table row count does not match the answers-only "
                    "subset of ResultSet.rows."
                ),
                metadata={
                    "rendered_row_count": len(table.rows),
                    "expected_row_count": len(expected_rows),
                },
            )
        ]

    for row_index, (parsed_cells, source_row) in enumerate(
        zip(table.rows, expected_rows, strict=True)
    ):
        for column, parsed_value in parsed_cells.items():
            expected = expected_cell(source_row, column)
            if expected is None:
                continue
            if parsed_value != expected:
                return [
                    _finding(
                        code="markdown_cell_drift",
                        dimension="surface_consistency",
                        message=(
                            f"Markdown table row {row_index} column "
                            f"{column!r}: rendered cell does not match "
                            f"the formatter applied to ResultSet.rows."
                        ),
                        metadata={
                            "row_index": row_index,
                            "column": column,
                            "rendered_cell": parsed_value,
                            "expected_cell": expected,
                        },
                    )
                ]
    return []


def _validate_csv_row_parity(
    plan: QueryPlan,
    result: "ResultSet",
) -> list[ValidationFinding]:
    """Assert csv_export rows project from the same source rows as result.rows.

    Compares a small subset of columns (`district_name`, `state`,
    `display_value`, `academic_year`) directly to row attributes. The CSV
    builder does no escaping, so the comparison is identity-by-value.
    Catches projection drift: a future CSV builder change that drops or
    transforms a column.

    Skips when there's no csv_export (some result types or test fixtures);
    skips when result.rows is empty.
    """
    if not result.rows:
        return []
    if result.csv_export is None:
        return []
    # #1514 D2: the CSV's data rows are the answers-only subset of result.rows
    # (the builders in artifacts/results.py filter on the shared
    # `is_rendered_answer_state` predicate; non-answer rows are voiced as
    # narrative sentences, never data points). Zip the CSV rows against that
    # SAME subset — the builder's exact filter, not a parallel one — with any
    # coverage-disclosure rows appended after the data rows left unchecked.
    #
    # Only check columns that are BOTH (a) in the csv row dict (the CSV
    # builder projected this attribute) AND (b) present as a row attribute
    # (the source has a value to compare against). Count-row CSVs, for
    # example, don't project `district_name` / `state` — comparing those
    # would false-positive.
    candidate_columns = (
        "district_id",
        "district_name",
        "state",
        "metric_id",
        "metric_name",
        "value",
        "display_value",
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
    )
    citation_by_marker = {citation.marker: citation for citation in result.citations}
    # The builders (_default/_trend/_count_csv_export_for_result) apply
    # `is_rendered_answer_state(row.coverage_state)` — a None state IS an
    # answer row and reaches the CSV. Anchor the zip to the same answers-only
    # subset via the one shared helper, so this parity side mirrors the
    # builders' surface exactly (never a parallel local filter).
    csv_source_rows = _expected_answer_rows(result)
    for row_index, (csv_row, source_row) in enumerate(
        zip(result.csv_export.rows, csv_source_rows, strict=False)
    ):
        expected_values = _expected_csv_values(source_row, citation_by_marker)
        for column in candidate_columns:
            if column not in csv_row:
                continue
            csv_value = csv_row[column]
            source_value = expected_values.get(column)
            if csv_value != source_value:
                return [
                    _finding(
                        code="csv_cell_drift",
                        dimension="surface_consistency",
                        message=(
                            f"CSV row {row_index} column {column!r}: exported "
                            f"value does not match ResultSet.rows."
                        ),
                        metadata={
                            "row_index": row_index,
                            "column": column,
                            "csv_value": csv_value,
                            "source_value": source_value,
                        },
                    )
                ]
    return []


def _expected_csv_values(
    source_row,
    citation_by_marker,
) -> dict[str, object]:
    return {
        "district_id": source_row.district_id,
        "district_name": source_row.district_name,
        "state": source_row.state,
        "metric_id": source_row.metric_id,
        "metric_name": source_row.metric_name,
        "value": source_row.value,
        "display_value": source_row.display_value,
        "academic_year": source_row.academic_year,
        "coverage_state": source_row.coverage_state,
        "coverage_display": source_row.coverage_display,
        "coverage_reason": source_row.coverage_reason,
        "coverage_qualifier": getattr(source_row, "coverage_qualifier", None),
        "coverage_prior_academic_year": getattr(
            source_row,
            "coverage_prior_academic_year",
            None,
        ),
        "coverage_prior_display_value": getattr(
            source_row,
            "coverage_prior_display_value",
            None,
        ),
        "citation_markers": " ".join(
            str(marker) for marker in source_row.citation_markers
        ),
        **_expected_source_metadata(source_row.citation_markers, citation_by_marker),
    }


def _expected_source_metadata(
    markers: list[int],
    citation_by_marker,
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


def _citation_valid_years(citations) -> tuple[str, str]:
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


def _validate_artifact_id_parity(
    plan: QueryPlan,
    result: "ResultSet",
    manifest_metadata: dict[str, object] | None,
) -> list[ValidationFinding]:
    """Assert manifest.metadata['artifact_id'] matches compute_artifact_id(result).

    Catches the case where the manifest carries a stamp from a stale or
    different ResultSet — e.g. the renderer was called twice with the same
    plan but different result objects, and the second result reached
    validation while the first's manifest was already cached.

    Skipped when:
    - `manifest_metadata` is None (no manifest emitted yet; pre-render path)
    - No `artifact_id` key (some test fixtures construct manifests directly)

    Both cases match the existing pre-render L1 invocation where this
    validator simply doesn't apply.
    """
    if manifest_metadata is None:
        return []
    stamped = manifest_metadata.get("artifact_id")
    if not isinstance(stamped, str):
        return []
    fresh = compute_artifact_id(result)
    if stamped != fresh:
        return [
            _finding(
                code="artifact_id_drift",
                dimension="surface_consistency",
                message=(
                    "ResultSet content does not match the artifact_id "
                    "stamped on manifest.metadata."
                ),
                metadata={
                    "stamped_artifact_id": stamped,
                    "fresh_artifact_id": fresh,
                },
            )
        ]
    return []


__all__ = [
    "_validate_artifact_id_parity",
    "_validate_csv_row_parity",
    "_validate_markdown_table_parity",
]
