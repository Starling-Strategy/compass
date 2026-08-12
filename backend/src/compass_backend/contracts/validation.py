"""Validation contracts for deterministic Compass artifacts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .planning import MetricRole

ValidationDimension = Literal[
    "selection",
    "metric",
    "filter",
    "data_fidelity",
    "denominator",
    "sort_order",
    "coverage_state",
    "citation_coverage",
    "surface_consistency",
    "numeric_token_provenance",
    "fact_coverage",
    "coverage_wording",
]
ValidationSeverity = Literal["info", "warning", "error"]
ValidationSelectionScope = Literal[
    "all_covered_districts",
    "state",
    "named_districts",
    "largest_districts",
]


class ResolvedMetricAuthority(BaseModel):
    """One catalog-resolved metric that validators may authorize."""

    model_config = ConfigDict(extra="forbid")

    metric_id: int
    metric_name: str = Field(min_length=1)
    role: MetricRole = "primary"
    metadata: dict[str, object] = Field(default_factory=dict)


class ResolvedSelectionAuthority(BaseModel):
    """Catalog-resolved selection authority for deterministic validation."""

    model_config = ConfigDict(extra="forbid")

    scope: ValidationSelectionScope
    district_ids: list[int] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)


class ResolvedProfileFieldAuthority(BaseModel):
    """Catalog-resolved NCES/profile field that validators may authorize."""

    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    input_phrase: str = Field(min_length=1)
    metadata: dict[str, object] = Field(default_factory=dict)


class ValidationAuthority(BaseModel):
    """Internal resolved-ID context produced by deterministic execution."""

    model_config = ConfigDict(extra="forbid")

    metrics: list[ResolvedMetricAuthority] = Field(default_factory=list)
    profile_fields: list[ResolvedProfileFieldAuthority] = Field(default_factory=list)
    selection: ResolvedSelectionAuthority | None = None


class ValidationFinding(BaseModel):
    """One deterministic validation finding."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    dimension: ValidationDimension
    severity: ValidationSeverity = "error"
    metadata: dict[str, object] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    """Validation report for one deterministic result artifact."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    dimensions_checked: list[ValidationDimension] = Field(default_factory=list)
    findings: list[ValidationFinding] = Field(default_factory=list)
