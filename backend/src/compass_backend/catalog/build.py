"""Builder for the chat-pipeline `CatalogResolver`.

Per SSN-233 ("How Compass Recognizes Things"), recognition authority lives in
governed catalog data and deterministic alias/topic/adjudicator layers — not
in an LLM that scans the full catalog. The earlier cached-catalog LLM resolver
was archived under `src/archive/compass_backend_cached_resolver/`. This builder
now wires the deterministic resolver only; the legitimate LLM role (the
Adjudicator, picking among a narrow candidate list) is set up inside
`CatalogResolver` itself.
"""

from __future__ import annotations

from compass_backend.config import Settings, settings as app_settings

from .recall import CatalogRecallService
from .resolution import CatalogRepository, CatalogResolver


def build_catalog_resolver(
    repository: CatalogRepository,
    *,
    settings: Settings = app_settings,
) -> CatalogResolver:
    """Construct a fully-wired `CatalogResolver` for the chat pipeline."""

    return CatalogResolver(
        repository,
        candidate_fusion_enabled=settings.catalog_candidate_fusion_enabled,
        topic_narrowing_enabled=settings.catalog_topic_narrowing_enabled,
        recall_service=(
            CatalogRecallService(repository)
            if settings.catalog_resolver_recall_enabled
            else None
        ),
    )


__all__ = ["build_catalog_resolver"]
