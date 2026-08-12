"""Tests for the unified CatalogReconciler.

Step 1 of the catalog-resolution-unification plan
(``docs/plans/2026-05-28-catalog-resolution-unification-plan.md``).
These tests pin the reconciler's behavior against the staging-replay
regression cases that motivated the plan:

- SORT-NEW-02 / case 8 — "Rank districts by enrollment, largest first."
  resolves ``enrollment`` to a ``profile_rank_field`` primary, not a
  metric-not-found block.
- Row 20 / case 13 — "free-and-reduced lunch share" resolves through
  the NCES alias path to ``profile_field:frpl_pct`` (and additionally
  to ``profile_rank_field:frpl_pct`` because that key is in the
  governed rank-fields enum).
- Family A bundle case — "starting salary" returns an ambiguous BA/MA
  metric bundle that the downstream adjudicator picks from.
- Unsupported-concept refusal — "union release time" surfaces as a
  blocker primary regardless of other catalog hits.
"""

from __future__ import annotations

import pytest

from compass_backend.catalog import (
    CatalogResolutionEntity,
    MetricBundleResolution,
    MetricCandidate,
    NCESFieldCandidate,
    ProfileFieldResolution,
)
from compass_backend.catalog.reconciliation import (
    CatalogReconciler,
    PhraseToReconcile,
)


class FakeCatalog:
    """Minimal catalog matching the reconciler's narrow Protocol.

    Returns scripted resolutions per phrase. Each branch encodes one
    real staging-replay signal.
    """

    async def resolve_metric_bundle(
        self,
        query: str,
        *,
        numeric_only: bool = False,
        limit: int = 5,
        degree_lane: object | None = None,
    ) -> MetricBundleResolution:
        normalized = query.casefold().strip()
        if normalized == "starting salary":
            return MetricBundleResolution(
                query=query,
                ambiguous=True,
                candidates=[
                    MetricCandidate(metric_id=89, name="BA starting salary"),
                    MetricCandidate(metric_id=96, name="MA starting salary"),
                ],
            )
        if normalized == "starting teacher salary ba":
            return MetricBundleResolution(
                query=query,
                resolved=[
                    MetricCandidate(metric_id=89, name="BA starting salary"),
                ],
            )
        return MetricBundleResolution(query=query)

    async def resolve_profile_field_authority(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> ProfileFieldResolution:
        normalized = query.casefold().strip()
        if normalized == "enrollment":
            return ProfileFieldResolution(
                query=query,
                resolved=NCESFieldCandidate(
                    field_key="enrollment",
                    label="NCES enrollment",
                    data_type="integer",
                ),
                resolution_method="exact",
            )
        if normalized == "frpl_pct":
            return ProfileFieldResolution(
                query=query,
                resolved=NCESFieldCandidate(
                    field_key="frpl_pct",
                    label="FRPL %",
                    data_type="percent",
                ),
                resolution_method="exact",
            )
        if normalized in {
            "free and reduced lunch share",
            "free-and-reduced lunch share",
            "frpl share",
        }:
            return ProfileFieldResolution(
                query=query,
                resolved=NCESFieldCandidate(
                    field_key="frpl_pct",
                    label="FRPL %",
                    data_type="percent",
                ),
                resolution_method="alias",
            )
        return ProfileFieldResolution(query=query)

    async def resolve_unsupported_concepts(
        self,
        phrases: list[str],
    ) -> list[CatalogResolutionEntity]:
        results: list[CatalogResolutionEntity] = []
        for phrase in phrases:
            if phrase.casefold().strip() == "union release time":
                results.append(
                    CatalogResolutionEntity(
                        input_phrase=phrase,
                        entity_type="unsupported_concept",
                        status="unsupported",
                        resolution_method="unsupported_catalog",
                        approved_key="union_release_time",
                        label="Union release time",
                        provenance="test",
                        message=(
                            "Compass does not yet have a governed metric for "
                            "union release time."
                        ),
                    )
                )
        return results


@pytest.fixture
def reconciler() -> CatalogReconciler:
    return CatalogReconciler(FakeCatalog())


@pytest.mark.asyncio
async def test_enrollment_resolves_to_profile_rank_field_for_sort_key(
    reconciler: CatalogReconciler,
) -> None:
    """SORT-NEW-02 / case 8 north-star case.

    "enrollment" with ``role_hint="sort_key"`` must return a
    ``profile_rank_field`` primary so the finalizer can route to
    ``rank_districts_by_profile_field`` instead of the metric path that
    currently refuses with "could not find a governed metric for
    'enrollment'".
    """

    result = await reconciler.reconcile("enrollment", role_hint="sort_key")

    assert result.primary is not None
    assert result.primary.entity_type == "profile_rank_field"
    assert result.primary.catalog_id == "profile_rank_field:enrollment"
    assert result.confidence == "high"


@pytest.mark.asyncio
async def test_enrollment_profile_subject_prefers_profile_field(
    reconciler: CatalogReconciler,
) -> None:
    """Role hint orders ties without inventing drawers.

    Both ``profile_rank_field`` and ``profile_field`` candidates exist
    for "enrollment". With ``role_hint="profile_subject"`` (lookup
    context, not rank), the NCES profile-field drawer wins.
    """

    result = await reconciler.reconcile(
        "enrollment", role_hint="profile_subject"
    )

    assert result.primary is not None
    assert result.primary.entity_type == "profile_field"
    drawer_types = {entity.entity_type for entity in result.candidates}
    assert {"profile_field", "profile_rank_field"}.issubset(drawer_types)


@pytest.mark.asyncio
async def test_frpl_share_resolves_via_alias_to_profile_rank_field(
    reconciler: CatalogReconciler,
) -> None:
    """Row 20 / case 13 regression.

    "free and reduced lunch share" must resolve through the NCES alias
    path (``resolve_profile_field_authority`` with
    ``resolution_method="alias"``). With ``role_hint="sort_key"`` the
    rankable drawer wins, mirroring the executor's
    ``rank_districts_by_profile_field("frpl_pct")`` path.
    """

    result = await reconciler.reconcile(
        "free and reduced lunch share", role_hint="sort_key"
    )

    assert result.primary is not None
    assert result.primary.entity_type == "profile_rank_field"
    assert result.primary.catalog_id == "profile_rank_field:frpl_pct"
    assert result.confidence == "high"


@pytest.mark.asyncio
async def test_frpl_share_profile_subject_uses_alias(
    reconciler: CatalogReconciler,
) -> None:
    """Alias-resolved profile field is reachable for non-rank contexts."""

    result = await reconciler.reconcile(
        "free and reduced lunch share", role_hint="profile_subject"
    )

    assert result.primary is not None
    assert result.primary.entity_type == "profile_field"
    assert result.primary.catalog_id == "profile_field:frpl_pct"
    assert (
        result.primary.source
        == "resolve_profile_field_authority/alias"
    )


@pytest.mark.asyncio
async def test_starting_salary_is_ambiguous_metric_bundle(
    reconciler: CatalogReconciler,
) -> None:
    """Multiple plausible candidates surface as ``ambiguous`` for adjudication.

    The bundle resolver returns BA and MA candidates without a single
    primary. Confidence reports ``ambiguous`` so the downstream
    adjudicator (step 2) is what picks.
    """

    result = await reconciler.reconcile(
        "starting salary", role_hint="metric_subject"
    )

    assert result.confidence == "ambiguous"
    metric_ids = {
        entity.metadata.get("metric_id")
        for entity in result.candidates
        if entity.entity_type == "metric"
    }
    assert {89, 96}.issubset(metric_ids)


@pytest.mark.asyncio
async def test_starting_teacher_salary_ba_resolves_to_single_metric(
    reconciler: CatalogReconciler,
) -> None:
    """Row 112 mention — specific BA-lane phrase resolves to one metric."""

    result = await reconciler.reconcile(
        "starting teacher salary BA", role_hint="metric_subject"
    )

    assert result.primary is not None
    assert result.primary.entity_type == "metric"
    assert result.primary.metadata.get("metric_id") == 89
    assert result.confidence == "high"


@pytest.mark.asyncio
async def test_unsupported_concept_blocks_all_other_candidates(
    reconciler: CatalogReconciler,
) -> None:
    """Out-of-universe alias is a hard refusal — wins regardless of role hint."""

    result = await reconciler.reconcile(
        "union release time", role_hint="metric_subject"
    )

    assert result.primary is not None
    assert result.primary.entity_type == "unsupported_concept"
    assert result.primary.catalog_id == "unsupported_concept:union_release_time"
    assert result.confidence == "high"


@pytest.mark.asyncio
async def test_unknown_phrase_returns_confidence_none(
    reconciler: CatalogReconciler,
) -> None:
    """Catalog gap surfaces as ``confidence="none"`` with empty candidates.

    Case 1123 ("salary schedule") is the staging example: the new
    pipeline distinguishes "no candidates" from a classification bug,
    so the gap becomes a backlog item rather than an opaque clarify
    loop.
    """

    result = await reconciler.reconcile(
        "something nobody has ever heard of", role_hint="metric_subject"
    )

    assert result.confidence == "none"
    assert result.primary is None
    assert result.candidates == ()


@pytest.mark.asyncio
async def test_empty_phrase_returns_confidence_none(
    reconciler: CatalogReconciler,
) -> None:
    result = await reconciler.reconcile("   ", role_hint="sort_key")

    assert result.confidence == "none"
    assert result.primary is None


@pytest.mark.asyncio
async def test_reconcile_all_deduplicates_case_insensitive(
    reconciler: CatalogReconciler,
) -> None:
    """Batch reconcile collapses duplicate phrases; first occurrence wins."""

    report = await reconciler.reconcile_all(
        [
            PhraseToReconcile(phrase="enrollment", role_hint="sort_key"),
            PhraseToReconcile(phrase="Enrollment", role_hint="profile_subject"),
            PhraseToReconcile(phrase="starting salary", role_hint="metric_subject"),
            PhraseToReconcile(phrase="", role_hint="sort_key"),
        ]
    )

    assert len(report.resolutions) == 2
    enrollment = report.for_phrase("enrollment")
    assert enrollment is not None
    assert enrollment.role_hint == "sort_key"
    assert report.for_phrase("STARTING SALARY") is not None


@pytest.mark.asyncio
async def test_reconciliation_report_distinguishes_blockers_from_gaps(
    reconciler: CatalogReconciler,
) -> None:
    """``has_blockers`` vs ``has_unresolved`` are independent signals.

    A blocker is an approved out-of-universe refusal (catalog says
    "we don't do this"); an unresolved phrase is a catalog data gap
    (no row anywhere). Downstream consumers route them differently.
    """

    report = await reconciler.reconcile_all(
        [
            PhraseToReconcile(phrase="union release time"),
            PhraseToReconcile(phrase="thing the catalog has no row for"),
        ]
    )

    assert report.has_blockers is True
    assert report.has_unresolved is True
