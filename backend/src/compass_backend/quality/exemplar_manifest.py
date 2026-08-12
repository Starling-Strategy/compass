"""Committed scorecard exemplar coverage contract and audit helpers."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from compass_backend.config import settings
from compass_backend.quality.scorecard_models import CANONICAL_DIM_SLUGS

DEFAULT_EXEMPLAR_MANIFEST_PATH = Path(__file__).with_name("scorecard_exemplars.yaml")

DIMENSION_NAME_BY_SLUG: dict[str, str] = {
    "selection-accuracy": "Selection Accuracy",
    "data-fidelity": "Data Fidelity",
    "coverage-state-labeling": "Coverage-State Labeling",
    "filter-accuracy": "Filter Accuracy",
    "sort-accuracy": "Sort Accuracy",
    "citation-accuracy": "Citation Accuracy",
    "consistency": "Consistency",
}
DIMENSION_SLUG_BY_NAME: dict[str, str] = {
    name: slug for slug, name in DIMENSION_NAME_BY_SLUG.items()
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DimensionExemplarRequirement(BaseModel):
    dimension_name: str
    minimum_active_cases: int = Field(ge=3)
    required_archetypes: tuple[str, ...] = Field(min_length=1)


class ExemplarManifest(BaseModel):
    version: str
    minimum_active_cases_per_dimension: int = Field(ge=3)
    dimensions: dict[str, DimensionExemplarRequirement]

    @model_validator(mode="after")
    def _validate_canonical_dimensions(self) -> "ExemplarManifest":
        actual = set(self.dimensions)
        expected = set(CANONICAL_DIM_SLUGS)
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            raise ValueError(
                "exemplar manifest dimensions must match canonical scorecard "
                f"dimensions; missing={missing} unknown={unknown}"
            )

        for slug, requirement in self.dimensions.items():
            if requirement.minimum_active_cases < self.minimum_active_cases_per_dimension:
                raise ValueError(
                    f"{slug} minimum_active_cases must be at least manifest minimum "
                    f"{self.minimum_active_cases_per_dimension}"
                )

        self.dimensions = {slug: self.dimensions[slug] for slug in CANONICAL_DIM_SLUGS}
        return self


class DimensionExemplarInventory(BaseModel):
    dim_slug: str
    dimension_name: str
    active_case_count: int = Field(ge=0)
    archetypes: tuple[str, ...] = ()


class DimensionExemplarAuditRow(BaseModel):
    dim_slug: str
    dimension_name: str
    required_case_count: int = Field(ge=0)
    active_case_count: int = Field(ge=0)
    missing_case_count: int = Field(ge=0)
    required_archetypes: tuple[str, ...]
    archetypes: tuple[str, ...]
    missing_archetypes: tuple[str, ...]
    status: Literal["complete", "incomplete"]


class ExemplarAudit(BaseModel):
    version: str
    passed: bool
    dimensions: list[DimensionExemplarAuditRow]


def _quote_ident(value: str) -> str:
    if not _IDENT_RE.fullmatch(value):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return f'"{value}"'


def load_exemplar_manifest(path: Path | None = None) -> ExemplarManifest:
    """Load and validate the committed exemplar manifest."""
    manifest_path = path or DEFAULT_EXEMPLAR_MANIFEST_PATH
    payload = yaml.safe_load(manifest_path.read_text()) or {}
    return ExemplarManifest.model_validate(payload)


def audit_exemplar_coverage(
    manifest: ExemplarManifest,
    inventory: list[DimensionExemplarInventory],
) -> ExemplarAudit:
    """Compare live exemplar inventory against the committed manifest."""
    inventory_by_slug = {row.dim_slug: row for row in inventory}
    rows: list[DimensionExemplarAuditRow] = []

    for slug in CANONICAL_DIM_SLUGS:
        requirement = manifest.dimensions[slug]
        live = inventory_by_slug.get(slug)
        active_case_count = live.active_case_count if live else 0
        archetypes = live.archetypes if live else ()
        archetype_set = set(archetypes)
        missing_archetypes = tuple(
            archetype
            for archetype in requirement.required_archetypes
            if archetype not in archetype_set
        )
        missing_case_count = max(
            requirement.minimum_active_cases - active_case_count,
            0,
        )
        status: Literal["complete", "incomplete"] = (
            "complete"
            if missing_case_count == 0 and not missing_archetypes
            else "incomplete"
        )
        rows.append(
            DimensionExemplarAuditRow(
                dim_slug=slug,
                dimension_name=requirement.dimension_name,
                required_case_count=requirement.minimum_active_cases,
                active_case_count=active_case_count,
                missing_case_count=missing_case_count,
                required_archetypes=requirement.required_archetypes,
                archetypes=archetypes,
                missing_archetypes=missing_archetypes,
                status=status,
            )
        )

    return ExemplarAudit(
        version=manifest.version,
        passed=all(row.status == "complete" for row in rows),
        dimensions=rows,
    )


async def load_exemplar_inventory(
    pool,
    pg_schema: str | None = None,
) -> list[DimensionExemplarInventory]:
    """Load active scorecard exemplar counts and archetype coverage from Compass."""
    schema = _quote_ident(pg_schema or settings.pg_schema)
    sql = f"""
        SELECT
            s.accuracy_primary_dimension AS dimension_name,
            COUNT(DISTINCT c.id)::int AS active_case_count,
            COALESCE(
                array_agg(DISTINCT c.metadata->>'scorecard_archetype')
                    FILTER (WHERE c.metadata->>'scorecard_archetype' IS NOT NULL),
                ARRAY[]::text[]
            ) AS archetypes
        FROM {schema}.cases c
        JOIN {schema}.scenarios s ON s.id = c.scenario_id
        WHERE c.active IS TRUE
          AND s.active IS TRUE
          AND c.metadata->>'scorecard_exemplar' = 'true'
        GROUP BY s.accuracy_primary_dimension
        ORDER BY s.accuracy_primary_dimension
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)

    inventory: list[DimensionExemplarInventory] = []
    for row in rows:
        dimension_name = row["dimension_name"]
        slug = DIMENSION_SLUG_BY_NAME.get(dimension_name)
        if slug is None:
            continue
        inventory.append(
            DimensionExemplarInventory(
                dim_slug=slug,
                dimension_name=dimension_name,
                active_case_count=row["active_case_count"],
                archetypes=tuple(sorted(row["archetypes"] or ())),
            )
        )
    return inventory


__all__ = [
    "DEFAULT_EXEMPLAR_MANIFEST_PATH",
    "DIMENSION_NAME_BY_SLUG",
    "DIMENSION_SLUG_BY_NAME",
    "DimensionExemplarAuditRow",
    "DimensionExemplarInventory",
    "DimensionExemplarRequirement",
    "ExemplarAudit",
    "ExemplarManifest",
    "audit_exemplar_coverage",
    "load_exemplar_inventory",
    "load_exemplar_manifest",
]
