"""Single-source manifest of metric-NAME strings embedded in planner snippets.

Several ``instructions/planner_guidance/*.md`` snippets carry *verbatim*
metric-name strings inside ``MetricSpec(name=...)`` / ``FilterSpec(field=...)``
prose and anchor lists. Catalog resolution matches these as exact text (an
exact ``navigator_metrics.name`` match, or an approved ``catalog_aliases``
alias, is what wins ranking in ``resolve_metric_bundle``), so they cannot be
de-literalized: a catalog rename would silently break planner routing.

This module is the typed home for those strings. The drift guard in
``test_snippet_metric_name_drift.py`` checks both halves of the single-source
contract: (1) every string here still appears verbatim in its snippet markdown
(offline — so this manifest cannot drift from the snippet), and (2) every
grounded string still resolves to a live catalog row on the surface tagged
below (live DB — so a catalog rename fails loudly).

Metric ids / alias canonical ids in the trailing comments were verified against
staging ``compass.navigator_metrics`` / ``compass.catalog_aliases`` (#1380).

Issue: #1380.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResolutionSurface(str, Enum):
    """Which deterministic catalog surface a snippet name resolves through.

    Mirrors the deterministic prefix of ``CatalogResolver.resolve_metric_bundle``
    (exact metric name first, then approved metric alias). ``FUZZY_ONLY`` names
    resolve through neither and reach a row only via the LLM adjudicator -- the
    guard records them but cannot deterministically pin them.
    """

    EXACT_METRIC_NAME = "exact_metric_name"
    METRIC_ALIAS = "metric_alias"
    FUZZY_ONLY = "fuzzy_only"


@dataclass(frozen=True)
class SnippetMetricName:
    """One embedded metric-name string plus where and how it resolves."""

    text: str
    snippet_filename: str
    surface: ResolutionSurface


_DIFFERENTIATED_PAY = "differentiated-pay-inventory.md"
_PARENTAL_LEAVE = "parental-leave-beyond-birthing.md"
_SICK_LEAVE = "sick-leave-ranking.md"
_SALARY_SCHEDULE = "salary-schedule-lookup.md"


# Names that MUST resolve to a live row on a DETERMINISTIC surface (exact
# navigator_metrics.name, or an approved metric catalog_alias). These are the
# strings the drift guard pins. Verified live (#1380) -> metric_id / alias
# canonical_id in trailing comments.
GROUNDED_SNIPPET_METRIC_NAMES: tuple[SnippetMetricName, ...] = (
    # --- differentiated-pay-inventory.md (exact navigator_metrics.name) ---
    SnippetMetricName(
        "Performance pay based on evaluation ratings, unrelated to salary schedule",
        _DIFFERENTIATED_PAY,
        ResolutionSurface.EXACT_METRIC_NAME,
    ),  # metric_id 170
    SnippetMetricName(
        "District offers additional pay for teaching in schools classified as hard-to-staff",
        _DIFFERENTIATED_PAY,
        ResolutionSurface.EXACT_METRIC_NAME,
    ),  # metric_id 175
    SnippetMetricName(
        'District offers additional pay for teaching subjects deemed "hard to staff"',
        _DIFFERENTIATED_PAY,
        ResolutionSurface.EXACT_METRIC_NAME,
    ),  # metric_id 179
    SnippetMetricName(
        "Additional pay for National Board Certification",
        _DIFFERENTIATED_PAY,
        ResolutionSurface.EXACT_METRIC_NAME,
    ),  # metric_id 174
    # --- parental-leave-beyond-birthing.md (exact navigator_metrics.name) ---
    # The 3 day-count strings below exist NOWHERE else in the repo -- this
    # snippet is their only home, so the guard is their only safety net.
    SnippetMetricName(
        "Who is eligible for paid parental leave?",
        _PARENTAL_LEAVE,
        ResolutionSurface.EXACT_METRIC_NAME,
    ),  # metric_id 216 (also the FilterSpec.field)
    SnippetMetricName(
        "Number of paid parental leave days beyond sick days granted to non-birthing parent",
        _PARENTAL_LEAVE,
        ResolutionSurface.EXACT_METRIC_NAME,
    ),  # metric_id 218
    SnippetMetricName(
        "Number of paid parental leave days beyond sick days granted to foster parent",
        _PARENTAL_LEAVE,
        ResolutionSurface.EXACT_METRIC_NAME,
    ),  # metric_id 219 -- string exists nowhere else
    SnippetMetricName(
        "Number of paid parental leave days beyond sick days granted to adoptive parent",
        _PARENTAL_LEAVE,
        ResolutionSurface.EXACT_METRIC_NAME,
    ),  # metric_id 220 -- string exists nowhere else
    # --- sick-leave-ranking.md (exact navigator_metrics.name; TX) ---
    SnippetMetricName(
        "Maximum number of annual paid sick days",
        _SICK_LEAVE,
        ResolutionSurface.EXACT_METRIC_NAME,
    ),  # metric_id 198
    SnippetMetricName(
        "Number of paid leave days granted in first year of employment if district "
        "does not distinguish between paid sick and personal leave days",
        _SICK_LEAVE,
        ResolutionSurface.EXACT_METRIC_NAME,
    ),  # metric_id 213
    SnippetMetricName(
        "Total number of paid sick and paid personal leave days granted in first "
        "year of employment if district does differentiate between personal and "
        "sick leave",
        _SICK_LEAVE,
        ResolutionSurface.EXACT_METRIC_NAME,
    ),  # metric_id 214
    # --- salary-schedule-lookup.md (approved metric ALIAS, not exact name) ---
    # The snippet says "Catalog resolution will canonicalize it" -- these are
    # catalog_aliases (entity_type='metric', approved), matched case-insensitively
    # (the alias row for the last one is stored "Maximum teacher salary").
    SnippetMetricName(
        "BA starting salary", _SALARY_SCHEDULE, ResolutionSurface.METRIC_ALIAS
    ),  # alias canonical_id 89
    SnippetMetricName(
        "MA starting salary", _SALARY_SCHEDULE, ResolutionSurface.METRIC_ALIAS
    ),  # alias canonical_id 96
    SnippetMetricName(
        "maximum teacher salary", _SALARY_SCHEDULE, ResolutionSurface.METRIC_ALIAS
    ),  # alias canonical_id 112
)


# Documented EXCEPTION (#1380): this string is emitted by the
# parental-leave-beyond-birthing.md negative-filter example as BOTH a
# MetricSpec.name and a FilterSpec.field with "use ... exactly", yet it matches
# NEITHER an exact navigator_metrics.name NOR an approved alias. The real row is
# "District offers paid parental leave beyond sick days" (metric_id 215); this
# phrasing reaches it only through the LLM adjudicator's fuzzy path. It is
# therefore genuinely fragile and cannot be pinned deterministically. Tracked
# here so the guard does not silently green-wash it; hardening it (alias the
# phrase to 215, or rewrite the snippet to the exact name) is a follow-up. The
# same string is also a runtime constant at planning/negative_filters.py.
FUZZY_ONLY_SNIPPET_METRIC_NAMES: tuple[SnippetMetricName, ...] = (
    SnippetMetricName(
        "Does the district offer paid parental leave?",
        _PARENTAL_LEAVE,
        ResolutionSurface.FUZZY_ONLY,
    ),
)
