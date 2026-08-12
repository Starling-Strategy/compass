"""Startup sanity check: warn on compass.criteria rows referencing unknown validators.

Run as a lifespan startup hook. Queries the DB for every distinct
`validator_name` referenced by `compass.criteria` rows with
`check_type='deterministic' AND active=TRUE`, diffs against
`validators.registry.known_validators()`, and emits a Logfire warning for any
unknown names. Never fails startup — operators may seed-then-implement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from compass_backend.quality.validators import registry  # populates registry on import

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequiredCatalogAlias:
    normalized_alias: str
    entity_type: str
    resolution_status: str


REQUIRED_M1_CATALOG_ALIASES: tuple[RequiredCatalogAlias, ...] = (
    RequiredCatalogAlias("starting salary", "metric", "ambiguous"),
    RequiredCatalogAlias("benefits", "metric_bundle", "approved"),
    RequiredCatalogAlias(
        "employee health insurance premium coverage",
        "metric",
        "approved",
    ),
    RequiredCatalogAlias("health insurance premiums", "metric", "approved"),
    RequiredCatalogAlias("school year length", "metric", "ambiguous"),
    RequiredCatalogAlias(
        "union release time",
        "unsupported_concept",
        "out_of_universe",
    ),
)


async def warn_on_unknown_deterministic_validators() -> None:
    """Lifespan hook: log a warning for any seeded validator_name without code."""

    try:
        from compass_backend.quality.verdict_pipeline import _get_or_create_pool
    except Exception:
        return

    try:
        pool = await _get_or_create_pool()
    except Exception:
        # Pool acquisition already logged downstream; treat as soft failure.
        return

    from compass_backend.config import settings

    schema = settings.pg_schema
    query = f"""
        SELECT DISTINCT payload->>'validator_name' AS validator_name
          FROM {schema}.criteria
         WHERE check_type = 'deterministic'
           AND active = TRUE
           AND payload ? 'validator_name'
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query)
    except Exception as exc:
        logger.debug(
            "validator-registry startup check: query failed (%s); skipping",
            exc,
        )
        return

    seeded = {r["validator_name"] for r in rows if r["validator_name"]}
    known = registry.known_validators()
    missing = sorted(seeded - known)
    if missing:
        logger.warning(
            "validator-registry: %d seeded validator_name(s) have no registered "
            "implementation — verdicts will be outcome='error'. Missing: %s. "
            "Known: %s",
            len(missing),
            missing,
            sorted(known),
        )


async def warn_on_missing_m1_catalog_aliases() -> None:
    """Lifespan hook: warn if governed launch aliases/blockers are missing."""

    try:
        from compass_backend.quality.verdict_pipeline import _get_or_create_pool
    except Exception:
        return

    try:
        pool = await _get_or_create_pool()
    except Exception:
        return

    from compass_backend.config import settings

    schema = settings.pg_schema
    aliases = sorted(
        {required.normalized_alias for required in REQUIRED_M1_CATALOG_ALIASES}
    )
    query = f"""
        SELECT normalized_alias, entity_type, resolution_status
          FROM {schema}.catalog_aliases
         WHERE active IS TRUE
           AND review_status = 'approved'
           AND normalized_alias = ANY($1::text[])
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, aliases)
    except Exception as exc:
        logger.debug(
            "m1 catalog-alias startup check: query failed (%s); skipping",
            exc,
        )
        return

    missing = missing_required_m1_catalog_aliases(rows)
    if missing:
        logger.warning(
            "catalog aliases: %d required M1 launch alias/blocker row(s) are "
            "missing from governed catalog data. Missing: %s",
            len(missing),
            [
                (
                    item.normalized_alias,
                    item.entity_type,
                    item.resolution_status,
                )
                for item in missing
            ],
        )


def missing_required_m1_catalog_aliases(rows: list[object]) -> list[RequiredCatalogAlias]:
    """Return required M1 alias rows absent from a DB result set."""

    present = {
        (
            _row_value(row, "normalized_alias"),
            _row_value(row, "entity_type"),
            _row_value(row, "resolution_status"),
        )
        for row in rows
    }
    return [
        required
        for required in REQUIRED_M1_CATALOG_ALIASES
        if (
            required.normalized_alias,
            required.entity_type,
            required.resolution_status,
        )
        not in present
    ]


async def warn_on_nces_allowlist_drift() -> None:
    """Lifespan hook: warn if the DB ``compass.nces_allowlist`` rows differ
    from the in-code ``DEFAULT_NCES_ALLOWLIST`` tuple.

    The sync test `test_nces_allowlist_in_sync_with_migration` enforces
    code/DB lockstep in CI. This runtime check catches the orthogonal
    case where the migration ran on staging/prod but the in-code tuple
    was not redeployed (or vice versa) — i.e., an environment that
    diverges from the committed pair. Warn-only today; flipping to fail
    is a separate audit P2 follow-up that pairs the two-deploy escalation
    rule from W2-M5-00a's plan entry.
    """

    try:
        from compass_backend.quality.verdict_pipeline import _get_or_create_pool
    except Exception:
        return
    try:
        pool = await _get_or_create_pool()
    except Exception:
        return

    from compass_backend.catalog.nces_allowlist import DEFAULT_NCES_ALLOWLIST
    from compass_backend.config import settings

    schema = settings.pg_schema
    query = f"""
        SELECT field_key
          FROM {schema}.nces_allowlist
         WHERE active IS TRUE
           AND review_status = 'approved'
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query)
    except Exception as exc:
        logger.debug(
            "nces-allowlist startup drift check: query failed (%s); skipping",
            exc,
        )
        return

    in_db = {_row_value(row, "field_key") for row in rows}
    in_code = {entry.field_key for entry in DEFAULT_NCES_ALLOWLIST if entry.active}
    missing_in_db = sorted(in_code - in_db)
    extra_in_db = sorted(in_db - in_code)
    if missing_in_db or extra_in_db:
        logger.warning(
            "nces-allowlist drift: %d field(s) missing from DB, %d extra in DB. "
            "Missing in DB: %s. Extra in DB: %s. Run migration 060 or redeploy.",
            len(missing_in_db),
            len(extra_in_db),
            missing_in_db,
            extra_in_db,
        )


async def warn_on_profile_rank_fields_drift() -> None:
    """Lifespan hook: warn if ``compass.profile_rank_fields`` rows differ from
    the in-code ``DEFAULT_PROFILE_RANK_FIELDS`` tuple.

    Same pattern as ``warn_on_nces_allowlist_drift``; catches deploy-mode
    drift between the migration and the in-code authority.
    """

    try:
        from compass_backend.quality.verdict_pipeline import _get_or_create_pool
    except Exception:
        return
    try:
        pool = await _get_or_create_pool()
    except Exception:
        return

    from compass_backend.catalog.profile_rank_fields import (
        DEFAULT_PROFILE_RANK_FIELDS,
    )
    from compass_backend.config import settings

    schema = settings.pg_schema
    query = f"""
        SELECT field_key
          FROM {schema}.profile_rank_fields
         WHERE active IS TRUE
           AND review_status = 'approved'
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query)
    except Exception as exc:
        logger.debug(
            "profile-rank-fields startup drift check: query failed (%s); skipping",
            exc,
        )
        return

    in_db = {_row_value(row, "field_key") for row in rows}
    in_code = {
        field.field_key for field in DEFAULT_PROFILE_RANK_FIELDS if field.active
    }
    missing_in_db = sorted(in_code - in_db)
    extra_in_db = sorted(in_db - in_code)
    if missing_in_db or extra_in_db:
        logger.warning(
            "profile-rank-fields drift: %d field(s) missing from DB, %d extra in DB. "
            "Missing in DB: %s. Extra in DB: %s. Run migration 061 or redeploy.",
            len(missing_in_db),
            len(extra_in_db),
            missing_in_db,
            extra_in_db,
        )


def _row_value(row: object, key: str) -> object:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]  # type: ignore[index]
    except Exception:
        return getattr(row, key, None)
