"""Public entry point for deterministic-result validators.

The validators themselves live in `quality.validators.{selection,metrics,coverage}`;
constants and shared helpers live in `quality._validation_helpers`. The split
was done in 2026-05 to break a 1,926-LOC monolith along dimension-group
boundaries. Behavior is unchanged; the module boundary is the only edit.

`validate_result()` is the only public symbol; downstream callers should
continue to import it from `compass_backend.quality.validation` or
`compass_backend.quality`.
"""

from __future__ import annotations

from compass_backend.artifacts import CompositeRankingResult, ResultSet
from compass_backend.contracts.planning import MetricSpec, QueryPlan
from compass_backend.contracts.validation import (
    ResolvedMetricAuthority,
    ValidationAuthority,
    ValidationDimension,
    ValidationReport,
)

from ._validation_helpers import _COUNT_DIMENSIONS, _FIRST_PASS_DIMENSIONS
from .validators.coverage import (
    _validate_citation_coverage,
    _validate_coverage_state,
    _validate_denominator,
    _validate_fact_coverage,
    _validate_forbidden_coverage_wording,
    _validate_lookup_criteria,
    _validate_numeric_token_provenance,
    _validate_sort_order,
    _validate_surface_consistency,
    _validate_temporal_data_fidelity,
)
from .validators.metrics import _validate_filter, _validate_metric
from .validators.selection import _validate_selection
from .validators.surface import (
    _validate_artifact_id_parity,
    _validate_csv_row_parity,
    _validate_markdown_table_parity,
)


def validate_result(
    plan: QueryPlan,
    result: ResultSet,
    *,
    authority: ValidationAuthority | None = None,
    rendered_body: str | None = None,
    manifest_metadata: dict[str, object] | None = None,
) -> ValidationReport:
    """Validate one deterministic result against the typed query plan.

    `rendered_body` and `manifest_metadata`, when provided, unlock the
    post-render surface_consistency checks: markdown-cell parity (needs
    `rendered_body`) and artifact_id parity (needs `manifest_metadata`).
    Pre-render callers omit both; the validators short-circuit silently.

    ``CompositeRankingResult`` envelopes (W2-M3-01, #806) are validated by
    delegating to ``validate_result`` on each child — the artifact contract
    promises each child is a fully validated ``MetricRankingResult`` and we
    enforce that promise here. The envelope's per-child authority and
    plan slice are reconstructed so per-result validators (e.g.,
    ``_validate_metric`` which requires exactly one primary metric) pass.
    Surface-consistency / markdown-table parity over the composite body
    is intentionally NOT routed through this path yet — that validator
    parses a single table and would need extending to N-table awareness;
    that's a separate quality follow-up.
    """

    if isinstance(result, CompositeRankingResult):
        return _validate_composite_ranking(plan, result, authority)

    findings = [
        *_validate_selection(plan, result, authority),
        *_validate_metric(plan, result, authority),
        *_validate_lookup_criteria(result),
        *_validate_filter(plan, result),
        *_validate_denominator(result),
        *_validate_temporal_data_fidelity(plan, result),
        *_validate_sort_order(plan, result),
        *_validate_surface_consistency(plan, result),
        *_validate_csv_row_parity(plan, result),
        *_validate_markdown_table_parity(plan, result, rendered_body),
        *_validate_artifact_id_parity(plan, result, manifest_metadata),
        *_validate_coverage_state(result),
        *_validate_citation_coverage(result),
        *_validate_numeric_token_provenance(result, rendered_body, plan),
        *_validate_fact_coverage(plan, result, rendered_body),
        *_validate_forbidden_coverage_wording(rendered_body),
    ]
    return ValidationReport(
        valid=not any(finding.severity == "error" for finding in findings),
        dimensions_checked=_dimensions_checked(
            plan,
            result,
            rendered_body,
            manifest_metadata,
        ),
        findings=findings,
    )


def _dimensions_checked(
    plan: QueryPlan,
    result: ResultSet,
    rendered_body: str | None,
    manifest_metadata: dict[str, object] | None,
) -> list[ValidationDimension]:
    if result.result_type == "metric_count":
        dimensions = list(_COUNT_DIMENSIONS)
        if _surface_consistency_dimension_checked(
            plan, result, rendered_body, manifest_metadata
        ):
            _insert_dimension_before(dimensions, "surface_consistency", "coverage_state")
        if rendered_body is not None:
            dimensions.append("numeric_token_provenance")
            dimensions.append("fact_coverage")
            dimensions.append("coverage_wording")
        return dimensions

    dimensions = list(_FIRST_PASS_DIMENSIONS)
    if _surface_consistency_dimension_checked(
        plan, result, rendered_body, manifest_metadata
    ):
        _insert_dimension_before(dimensions, "surface_consistency", "coverage_state")
    if result.result_type == "metric_trend":
        dimensions.append("data_fidelity")
    if rendered_body is not None:
        dimensions.append("numeric_token_provenance")
        dimensions.append("fact_coverage")
        dimensions.append("coverage_wording")
    return dimensions


def _insert_dimension_before(
    dimensions: list[ValidationDimension],
    dimension: ValidationDimension,
    before: ValidationDimension,
) -> None:
    if dimension in dimensions:
        return
    if before in dimensions:
        dimensions.insert(dimensions.index(before), dimension)
        return
    dimensions.append(dimension)


def _surface_consistency_dimension_checked(
    plan: QueryPlan,
    result: ResultSet,
    rendered_body: str | None,
    manifest_metadata: dict[str, object] | None,
) -> bool:
    if _unbounded_ranking_surface_check_applies(plan, result):
        return True
    if result.rows and result.csv_export is not None:
        return True
    if result.rows and rendered_body is not None:
        return True
    if manifest_metadata is not None and isinstance(
        manifest_metadata.get("artifact_id"),
        str,
    ):
        return True
    return False


def _unbounded_ranking_surface_check_applies(
    plan: QueryPlan,
    result: ResultSet,
) -> bool:
    return (
        result.result_type == "metric_ranking"
        and (plan.limit is None or plan.limit.kind == "all")
        and plan.selection.scope in {"all_covered_districts", "state"}
    )


def _validate_composite_ranking(
    plan: QueryPlan,
    result: CompositeRankingResult,
    authority: ValidationAuthority | None,
) -> ValidationReport:
    """Validate each child as a stand-alone ``MetricRankingResult`` and
    aggregate findings + dimensions across children.

    For each child we build a tailored slice of the input:
    - ``child_plan`` carries one ``MetricSpec`` (the child's metric) and
      ``requires_composite_ranking=False`` so per-validator branches treat
      it as an ordinary single-metric rank.
    - ``child_authority`` is narrowed to the matching metric (matched by
      ``metric_id`` from the child's first row, falling back to position
      if the envelope's authority list is empty or shorter than children).
    """

    all_findings: list = []
    all_dimensions: list[ValidationDimension] = []

    authority_by_id: dict[int, ResolvedMetricAuthority] = {}
    if authority is not None:
        for metric in authority.metrics:
            authority_by_id.setdefault(metric.metric_id, metric)

    for child in result.children:
        child_metric_name = (
            child.rows[0].metric_name if child.rows else plan.metrics[0].name
        )
        child_metric_id = child.rows[0].metric_id if child.rows else None
        child_plan = plan.model_copy(
            update={
                "metrics": [MetricSpec(name=child_metric_name)],
                "requires_composite_ranking": False,
            }
        )
        child_authority: ValidationAuthority | None = None
        if authority is not None:
            matched_metric = (
                authority_by_id.get(child_metric_id)
                if child_metric_id is not None
                else None
            )
            if matched_metric is None and authority.metrics:
                matched_metric = authority.metrics[0]
            child_authority = authority.model_copy(
                update={
                    "metrics": (
                        [matched_metric.model_copy(update={"role": "primary"})]
                        if matched_metric is not None
                        else []
                    )
                }
            )
        child_report = validate_result(
            child_plan,
            _child_result_with_referenced_citations(child),
            authority=child_authority,
        )
        all_findings.extend(child_report.findings)
        for dim in child_report.dimensions_checked:
            if dim not in all_dimensions:
                all_dimensions.append(dim)

    return ValidationReport(
        valid=not any(finding.severity == "error" for finding in all_findings),
        dimensions_checked=all_dimensions,
        findings=all_findings,
    )


def _child_result_with_referenced_citations(child):
    """Validate a composite child against only citations its own rows reference."""

    referenced_markers = {
        marker for row in child.rows for marker in row.citation_markers
    }
    if not referenced_markers:
        return child.model_copy(update={"citations": []})
    citations = [
        citation
        for citation in child.citations
        if citation.marker in referenced_markers
    ]
    payload = child.model_dump()
    payload["citations"] = citations
    payload["csv_export"] = None
    payload["chart"] = None
    return type(child)(**payload)
