"""Drift guard: snippet-embedded metric-name strings stay in sync (#1380).

Four ``instructions/planner_guidance/*.md`` snippets carry verbatim metric-name
strings that catalog resolution matches as exact text. They cannot be
de-literalized, so this guard keeps the chain snippet <-> manifest <-> catalog
in sync and fails loudly when any link drifts:

  1. ``test_manifest_strings_appear_in_snippet_markdown`` (OFFLINE, default
     suite): every manifested string still appears verbatim in its snippet, so
     a snippet edit that renames/drops a string fails fast in normal CI.
  2. ``test_grounded_snippet_names_resolve_to_live_catalog_rows`` (LIVE DB):
     every grounded string still resolves on its tagged deterministic surface
     -- an exact ``navigator_metrics.name`` row, or an approved
     ``catalog_aliases`` metric alias -- so a catalog rename fails loudly.

The live tests mirror only the *deterministic* prefix of
``CatalogResolver.resolve_metric_bundle`` (exact name, then approved alias);
they never reach the LLM adjudicator, so they cost nothing and are
deterministic. They are gated by ``--run-live-db`` and marked ``live_db`` so the
default offline suite stays green and DB-free, while the cheap markdown-drift
check above runs everywhere.

Run the live half with:
    op run --env-file=.env.op -- env PYTHONPATH=src uv run pytest \
        src/compass_backend/tests/test_snippet_metric_name_drift.py \
        --tb=short --run-live-db
"""

from __future__ import annotations

import pytest

from compass_backend.config import Settings
from compass_backend.db import ReadOnlyCatalogRepository
from compass_backend.db._pool import create_chat_pool
from compass_backend.instructions.loader import load_planner_guidance
from compass_backend.tests.snippet_metric_names import (
    FUZZY_ONLY_SNIPPET_METRIC_NAMES,
    GROUNDED_SNIPPET_METRIC_NAMES,
    ResolutionSurface,
    SnippetMetricName,
)

ALL_SNIPPET_NAMES: tuple[SnippetMetricName, ...] = (
    GROUNDED_SNIPPET_METRIC_NAMES + FUZZY_ONLY_SNIPPET_METRIC_NAMES
)


def _live_settings(request: pytest.FixtureRequest) -> Settings:
    if not request.config.getoption("--run-live-db"):
        pytest.skip("pass --run-live-db to run live DB drift checks")
    settings = Settings()
    if not settings.pg_password.get_secret_value():
        pytest.skip("PG_PASSWORD is required for live DB drift checks")
    return settings


# --- Offline: manifest mirrors the snippet markdown -------------------------


@pytest.mark.parametrize(
    "entry", ALL_SNIPPET_NAMES, ids=[e.text[:48] for e in ALL_SNIPPET_NAMES]
)
def test_manifest_strings_appear_in_snippet_markdown(entry: SnippetMetricName) -> None:
    """Every manifested string is still present verbatim in its snippet.

    Needs no DB, so it runs in the default suite. If a snippet edit renames or
    drops one of these strings, this fails -- the manifest (and so the live
    drift guard) must be updated in lockstep with the snippet.
    """

    # The manifest holds the canonical (catalog) form with raw quotes. Snippets
    # embed names inside `name="..."`, escaping any inner quote as \" — unescape
    # that back to the canonical form before comparing (only metric 179 carries
    # inner quotes; the replace is a no-op for every other name).
    body = load_planner_guidance(entry.snippet_filename).replace('\\"', '"')
    assert entry.text in body, (
        f"{entry.text!r} is no longer present verbatim in "
        f"{entry.snippet_filename}; update snippet_metric_names.py to match the "
        f"snippet (or fix the snippet if the change was unintended)."
    )


# --- Live DB: snippet names still resolve in the catalog --------------------


@pytest.mark.live_db
@pytest.mark.asyncio
async def test_grounded_snippet_names_resolve_to_live_catalog_rows(
    request: pytest.FixtureRequest,
) -> None:
    """Every EXACT_METRIC_NAME / METRIC_ALIAS string resolves to a live row.

    A catalog rename that orphans any of these strings fails here, loudly,
    instead of silently breaking the planner routing the snippet teaches.
    """

    settings = _live_settings(request)
    pool = await create_chat_pool(settings)
    try:
        repository = ReadOnlyCatalogRepository(source=settings, pool=pool)

        # Exact navigator_metrics.name surface: load the full metric list once
        # (the same call the cached resolver uses) and match case-insensitively,
        # exactly as search_metrics ranks an exact name first.
        metrics = await repository.fetch_all_metrics_for_resolver()
        metric_names = {m.name.casefold() for m in metrics}

        failures: list[str] = []
        for entry in GROUNDED_SNIPPET_METRIC_NAMES:
            if entry.surface is ResolutionSurface.EXACT_METRIC_NAME:
                if entry.text.casefold() not in metric_names:
                    failures.append(
                        f"[exact name] {entry.text!r} (from {entry.snippet_filename}) "
                        f"has no compass.navigator_metrics row"
                    )
            elif entry.surface is ResolutionSurface.METRIC_ALIAS:
                aliases = await repository.search_catalog_aliases(
                    entry.text, entity_types={"metric"}
                )
                if not any(a.approved for a in aliases):
                    failures.append(
                        f"[metric alias] {entry.text!r} (from {entry.snippet_filename}) "
                        f"has no approved compass.catalog_aliases metric row"
                    )
            else:  # pragma: no cover - manifest invariant
                failures.append(
                    f"{entry.text!r} carries non-deterministic surface "
                    f"{entry.surface!r}; it belongs in FUZZY_ONLY, not the grounded set"
                )

        assert not failures, "snippet metric-name drift detected:\n" + "\n".join(
            failures
        )
    finally:
        await pool.close()


@pytest.mark.live_db
@pytest.mark.asyncio
async def test_fuzzy_only_snippet_names_are_documented_not_grounded(
    request: pytest.FixtureRequest,
) -> None:
    """Document the known fragile string instead of silently green-washing it.

    'Does the district offer paid parental leave?' is emitted by the
    parental-leave snippet with 'use ... exactly' yet matches neither an exact
    navigator_metrics.name nor an approved alias (the real row is
    'District offers paid parental leave beyond sick days', metric_id 215). It
    reaches a row only through the LLM adjudicator. This pins that the string is
    genuinely NOT deterministically resolvable, so if a future catalog/alias
    change makes it exactly resolvable, this fails and the name should be
    promoted into GROUNDED_SNIPPET_METRIC_NAMES.
    """

    settings = _live_settings(request)
    pool = await create_chat_pool(settings)
    try:
        repository = ReadOnlyCatalogRepository(source=settings, pool=pool)
        metrics = await repository.fetch_all_metrics_for_resolver()
        metric_names = {m.name.casefold() for m in metrics}

        for entry in FUZZY_ONLY_SNIPPET_METRIC_NAMES:
            exact = entry.text.casefold() in metric_names
            aliases = await repository.search_catalog_aliases(
                entry.text, entity_types={"metric"}
            )
            approved = any(a.approved for a in aliases)
            if exact or approved:
                pytest.fail(
                    f"{entry.text!r} is now deterministically resolvable "
                    f"(exact={exact}, alias={approved}); promote it into "
                    f"GROUNDED_SNIPPET_METRIC_NAMES and pin it."
                )
    finally:
        await pool.close()
