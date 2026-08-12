"""Read-only catalog repository over Compass navigator tables."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import asyncpg


# ---------------------------------------------------------------------------
# Process-lifetime alias cache (C1 — #1357)
#
# ``catalog_aliases`` rows (active + approved) are static-per-deploy reference
# data: phrase-to-entity recognition maps used ~15×/turn for recall and
# resolution. Loading them once and filtering in-memory saves ~15 pool
# acquires/turn with no correctness trade-off — the cache is byte-identical to
# the live query results for the same filter predicates.
#
# Cache invalidation: restart the process, or call ``reset_alias_cache()``
# (exposed for tests and future admin hooks). Catalog edits will not be visible
# until the next restart or explicit reset.
# ---------------------------------------------------------------------------

_alias_cache: list["CatalogAliasRecord"] | None = None
_alias_cache_lock: asyncio.Lock = asyncio.Lock()


def reset_alias_cache() -> None:
    """Clear the process-lifetime alias cache.

    The next call to ``search_catalog_aliases`` or ``fetch_recall_aliases``
    will reload from the database. Call this after catalog edits during a
    running process, or from tests to prevent cross-test pollution.
    """
    global _alias_cache
    _alias_cache = None


# ---------------------------------------------------------------------------
# Process-lifetime NCES column cache (C2 — #1357)
#
# ``search_nces_fields`` introspects ``information_schema.columns`` for the
# ``nces_districts`` table ~3-5×/turn.  The column set is static-per-deploy
# (schema changes require a migration and restart), so caching it per
# ``(schema, table)`` key is correct and reduces pool acquires to 1 per
# process lifetime.
#
# Cache invalidation: restart the process, or call ``reset_nces_fields_cache()``
# (exposed for tests and future admin hooks). Schema changes will not be
# visible until the next restart or explicit reset.
# ---------------------------------------------------------------------------

_nces_fields_cache: dict[tuple[str, str], frozenset[str]] = {}
_nces_fields_cache_lock: asyncio.Lock = asyncio.Lock()


def reset_nces_fields_cache() -> None:
    """Clear the process-lifetime NCES column introspection cache.

    The next call to ``search_nces_fields`` will re-query
    ``information_schema.columns``. Call this after a schema migration
    during a running process, or from tests to prevent cross-test pollution.
    """
    _nces_fields_cache.clear()


from compass_backend.catalog import (
    CatalogAliasRecord,
    CoveredNCESProfileRow,
    DistrictCandidate,
    DistrictResolution,
    GlossaryTermCandidate,
    MetricCandidate,
    NCESFieldCandidate,
    NcesDistrictMatch,
    ProfileFieldValueRow,
    RendererNote,
    SourceDocumentCandidate,
    TopicCandidate,
    TopicContentLink,
    normalize_district_name_for_resolution,
)
from compass_backend.catalog.nces_allowlist import DEFAULT_NCES_ALLOWLIST
from compass_backend.catalog.profile_rank_fields import (
    DEFAULT_PROFILE_RANK_FIELDS,
    UnsupportedRankField,
)
from compass_backend.config import Settings, settings
from compass_backend.db._base import RepositoryBase
from compass_backend.db._pool import ChatPoolHolder

_NCES_FIELDS: tuple[NCESFieldCandidate, ...] = tuple(
    NCESFieldCandidate(
        field_key=entry.field_key,
        label=entry.label,
        data_type=entry.data_type,
        description=entry.description or None,
    )
    for entry in DEFAULT_NCES_ALLOWLIST
    if entry.active
)

_METRIC_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_PROFILE_TOKEN_RE = re.compile(r"[^a-z0-9]+")
# Inverse intent to `catalog/degree_lane.py`: this set STRIPS the degree
# tokens ("ba", "bachelor", "bachelors", and the symmetric "ma" handling at
# the resolver level) from free-text search so an ambiguous query like
# "bachelor's starting salary" still retrieves BA and MA candidates for the
# resolver to disambiguate. `catalog.degree_lane.classify_metric_lane` is
# the opposite operation — it classifies a metric *name* into a lane for
# selection. The two must stay in sync; if a future PR adds another degree
# token to one, audit whether it also belongs in the other. See audit #17.
_METRIC_STOPWORDS = {
    "a",
    "all",
    "an",
    "and",
    "are",
    "average",
    "by",
    "ba",
    "bachelor",
    "bachelors",
    "degree",
    "district",
    "districts",
    "do",
    "does",
    "for",
    "in",
    "non",
    "of",
    "per",
    "policies",
    "policy",
    "the",
    "teacher",
    "teachers",
    "with",
    "year",
}
_PROFILE_FIELD_STOPWORDS = {
    "a",
    "an",
    "and",
    "by",
    "district",
    "districts",
    "field",
    "for",
    "in",
    "nces",
    "of",
    "profile",
    "school",
    "schools",
    "size",
    "the",
}
_DISTRICT_STOPWORDS = {
    "am",
    "and",
    "are",
    "district",
    "districts",
    "from",
    "i",
    "in",
    "me",
    "our",
    "public",
    "school",
    "schools",
    "the",
    "we",
    "work",
}

_PROFILE_RANK_FIELDS: dict[str, dict[str, object]] = {
    field.field_key: {
        "column": field.column_name,
        "label": field.label,
        "metric_id": field.metric_id,
        "display": field.display,
        "vintage_label": field.vintage_label,
    }
    for field in DEFAULT_PROFILE_RANK_FIELDS
    if field.active
}

_NCES_FIELD_BY_KEY = {field.field_key: field for field in _NCES_FIELDS}
_NCES_FIELD_DISPLAY = {
    "enrollment": "integer",
    "teachers_fte": "number",
    "pupil_teacher_ratio": "number",
    "number_of_schools": "integer",
    "locale_text": "text",
    "total_rev_pp": "currency",
    "total_exp_pp": "currency",
    # frpl_pct is the one allowlisted profile field that lives only in
    # navigator_districts (not nces_districts) — see _NAVIGATOR_PROFILE_FIELDS.
    # Omitting it here made lookup_nces_profile_values early-return [] and the
    # turn refuse with "could not resolve sourced NCES/profile values" for a
    # field whose data is present for 99.4% of districts (#1720 follow-up).
    "frpl_pct": "percent",
}

# Allowlisted profile fields whose value lives ONLY in navigator_districts, not
# nces_districts. lookup_nces_profile_values sources these via the district_id
# link (mirroring the frpl_authority CTE in list_covered_nces_profiles) instead
# of selecting nces.<field_key>, which would be a column-does-not-exist error.
_NAVIGATOR_PROFILE_FIELDS = frozenset({"frpl_pct"})


def quote_ident(identifier: str) -> str:
    """Quote a PostgreSQL identifier."""

    return '"' + identifier.replace('"', '""') + '"'


class ReadOnlyCatalogRepository(RepositoryBase):
    """Resolve Compass catalog entities from navigator tables.

    Every query borrows a connection from the shared chat pool. Production
    wires the singleton pool through ``create_app_from_settings``; tests
    that need a synthetic pool construct a ``FakeChatPool`` from
    ``compass_backend.tests._chat_pool_fixtures``.
    """

    def __init__(
        self,
        source: Settings = settings,
        *,
        pool: asyncpg.Pool | ChatPoolHolder,
    ) -> None:
        self._settings = source
        self._pool = pool

    async def search_metrics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[MetricCandidate]:
        """Search metric catalog rows without hardcoded metric IDs."""

        tokens = _metric_search_tokens(query)
        sql = f"""
            WITH candidate_metrics AS (
                SELECT
                    metric.metric_id,
                    metric.name,
                    metric.topic,
                    metric.answer_type,
                    LOWER(
                        metric.name || ' ' ||
                        COALESCE(metric.topic, '') || ' ' ||
                        COALESCE(metric.subtopic, '')
                    ) AS search_text
                FROM {self._table("navigator_metrics")} metric
            )
            SELECT
                metric.metric_id,
                metric.name,
                metric.topic,
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM {self._table("navigator_answers")} answer
                        WHERE answer.metric_id = metric.metric_id
                          AND answer.value ~
                              '^[[:space:]]*[$]?[[:space:]]*-?[0-9][0-9,]*([.][0-9]+)?[[:space:]]*%?[[:space:]]*$'
                    )
                    THEN 'numeric'
                    ELSE metric.answer_type
                END AS answer_type
            FROM candidate_metrics metric
            CROSS JOIN LATERAL (
                SELECT COUNT(*)::int AS token_match_count
                FROM UNNEST($2::text[]) AS query_token(token)
                WHERE metric.search_text LIKE '%' || query_token.token || '%'
            ) token_score
            WHERE LOWER(metric.name) LIKE '%' || LOWER($1) || '%'
               OR LOWER(COALESCE(metric.topic, '')) LIKE '%' || LOWER($1) || '%'
               OR (
                    CARDINALITY($2::text[]) > 0
                    AND token_score.token_match_count >= LEAST(
                        CARDINALITY($2::text[]),
                        GREATEST(
                            2,
                            CEIL(CARDINALITY($2::text[]) * 0.6)::int
                        )
                    )
               )
            ORDER BY
                CASE
                    WHEN LOWER(metric.name) = LOWER($1) THEN 0
                    WHEN LOWER(metric.name) LIKE LOWER($1) || '%' THEN 1
                    WHEN LOWER(metric.name) LIKE '%' || LOWER($1) || '%' THEN 2
                    WHEN token_score.token_match_count = CARDINALITY($2::text[]) THEN 3
                    WHEN CARDINALITY($2::text[]) > 0 THEN 4
                    ELSE 5
                END,
                token_score.token_match_count DESC,
                metric.metric_id ASC
            LIMIT $3
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, query, tokens, limit)
        return [MetricCandidate(**dict(row)) for row in rows]

    async def fetch_metrics_by_ids(
        self,
        metric_ids: list[int],
    ) -> list[MetricCandidate]:
        """Fetch metric catalog rows by reviewed metric IDs."""

        if not metric_ids:
            return []
        sql = f"""
            SELECT
                metric.metric_id,
                metric.name,
                metric.topic,
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM {self._table("navigator_answers")} answer
                        WHERE answer.metric_id = metric.metric_id
                          AND answer.value ~
                              '^[[:space:]]*[$]?[[:space:]]*-?[0-9][0-9,]*([.][0-9]+)?[[:space:]]*%?[[:space:]]*$'
                    )
                    THEN 'numeric'
                    ELSE metric.answer_type
                END AS answer_type
            FROM {self._table("navigator_metrics")} metric
            WHERE metric.metric_id = ANY($1::int[])
            ORDER BY array_position($1::int[], metric.metric_id)
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, metric_ids)
        return [MetricCandidate(**dict(row)) for row in rows]

    async def _ensure_alias_cache(self) -> list["CatalogAliasRecord"]:
        """Return the process-lifetime alias cache, loading it on first call.

        Loads all ``active IS TRUE AND review_status='approved'`` rows once and
        stores them module-globally so subsequent calls filter in-memory.
        The ``asyncio.Lock`` prevents a thundering-herd double-load on startup.
        """
        global _alias_cache
        if _alias_cache is not None:
            return _alias_cache
        async with _alias_cache_lock:
            # Re-check after acquiring the lock — another coroutine may have
            # loaded the cache while we were waiting.
            if _alias_cache is not None:
                return _alias_cache
            sql = f"""
                SELECT
                    alias,
                    normalized_alias,
                    entity_type,
                    resolution_status,
                    canonical_id,
                    COALESCE(canonical_ids, '[]'::jsonb) AS canonical_ids,
                    COALESCE(candidate_refs, '[]'::jsonb) AS candidate_refs,
                    COALESCE(metadata, '{{}}'::jsonb) AS metadata,
                    source,
                    provenance,
                    scenario_ids,
                    review_status,
                    active
                FROM {self._table("catalog_aliases")}
                WHERE active IS TRUE
                  AND review_status = 'approved'
            """
            async with self._acquire() as conn:
                rows = await conn.fetch(sql)
            _alias_cache = [
                CatalogAliasRecord(
                    **{
                        **(data := dict(row)),
                        "canonical_ids": _jsonb_list(data["canonical_ids"]),
                        "candidate_refs": _jsonb_list(data["candidate_refs"]),
                        "metadata": _jsonb_object(data.get("metadata")),
                        "scenario_ids": data["scenario_ids"] or [],
                    }
                )
                for row in rows
            ]
            return _alias_cache

    async def search_catalog_aliases(
        self,
        alias: str,
        *,
        entity_types: set[str],
    ) -> list[CatalogAliasRecord]:
        """Search reviewed governed-recognition aliases.

        Results are filtered in-memory from the process-lifetime alias cache
        (loaded once on first call). Ordering replicates the SQL ``CASE
        resolution_status`` / ``alias`` sort so results are byte-identical to
        the live query.
        """

        normalized_aliases = set(_catalog_alias_lookup_values(alias))
        cache = await self._ensure_alias_cache()
        matches = [
            rec
            for rec in cache
            if rec.normalized_alias in normalized_aliases
            and rec.entity_type in entity_types
        ]
        # Replicate: ORDER BY CASE resolution_status ... END, alias
        _SEARCH_STATUS_ORDER = {
            "approved": 0,
            "ambiguous": 1,
            "conditional": 2,
            "out_of_universe": 3,
        }
        matches.sort(
            key=lambda r: (
                _SEARCH_STATUS_ORDER.get(r.resolution_status, 4),
                r.alias,
            )
        )
        return matches

    async def fetch_recall_aliases(
        self,
        entity_types: set[str],
    ) -> list[CatalogAliasRecord]:
        """Fetch active reviewed aliases available to broaden recall candidates.

        Results are filtered in-memory from the process-lifetime alias cache.
        Ordering replicates the SQL ``entity_type`` / ``CASE resolution_status``
        / ``normalized_alias`` / ``alias`` sort so results are byte-identical to
        the live query.
        """

        _RECALL_STATUSES = {"approved", "ambiguous", "out_of_universe"}
        cache = await self._ensure_alias_cache()
        matches = [
            rec
            for rec in cache
            if rec.resolution_status in _RECALL_STATUSES
            and rec.entity_type in entity_types
        ]
        # Replicate: ORDER BY entity_type, CASE resolution_status ... END,
        #            normalized_alias, alias
        _RECALL_STATUS_ORDER = {
            "approved": 0,
            "ambiguous": 1,
            "out_of_universe": 2,
        }
        matches.sort(
            key=lambda r: (
                r.entity_type,
                _RECALL_STATUS_ORDER.get(r.resolution_status, 3),
                r.normalized_alias,
                r.alias,
            )
        )
        return matches

    async def fetch_all_metrics_for_resolver(self) -> list[MetricCandidate]:
        """Fetch every catalog metric in deterministic order for the cached resolver.

        Result is sorted by ``metric_id`` so that catalog serialization is
        byte-stable across calls (Anthropic's content-hash cache requires it).
        """

        sql = f"""
            SELECT
                metric.metric_id,
                metric.name,
                metric.topic,
                metric.answer_type
            FROM {self._table("navigator_metrics")} metric
            ORDER BY metric.metric_id ASC
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql)
        return [MetricCandidate(**dict(row)) for row in rows]

    async def fetch_renderer_notes(self, note_keys: list[str]) -> list[RendererNote]:
        """Fetch approved renderer notes by governed note key."""

        if not note_keys:
            return []
        sql = f"""
            SELECT
                note_key,
                note_text,
                source,
                provenance,
                scenario_ids,
                review_status,
                active
            FROM {self._table("renderer_notes")}
            WHERE active IS TRUE
              AND review_status = 'approved'
              AND note_key = ANY($1::text[])
            ORDER BY array_position($1::text[], note_key)
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, note_keys)
        return [RendererNote(**dict(row)) for row in rows]

    async def resolve_districts(
        self,
        names: list[str],
        *,
        states: set[str] | None = None,
    ) -> DistrictResolution:
        """Resolve covered district names by exact or normalized name."""

        state_values = sorted(state.upper() for state in states or set())
        if state_values:
            sql = f"""
                SELECT
                    district_id,
                    name AS district_name,
                    state_abbrev AS state
                FROM {self._table("navigator_districts")}
                WHERE UPPER(state_abbrev) = ANY($1::text[])
                ORDER BY name, district_id
            """
            params: tuple[object, ...] = (state_values,)
        else:
            sql = f"""
                SELECT
                    district_id,
                    name AS district_name,
                    state_abbrev AS state
                FROM {self._table("navigator_districts")}
                ORDER BY name, district_id
            """
            params = ()

        async with self._acquire() as conn:
            rows = await conn.fetch(sql, *params)

        index: dict[str, list[DistrictCandidate]] = {}
        seen: set[tuple[int, str, str | None]] = set()
        for row in rows:
            candidate = DistrictCandidate(**dict(row))
            identity = (
                candidate.district_id,
                candidate.district_name,
                candidate.state,
            )
            if identity in seen:
                continue
            seen.add(identity)
            normalized = normalize_district_name_for_resolution(
                candidate.district_name
            )
            if normalized:
                index.setdefault(normalized, []).append(candidate)

        resolved: list[DistrictCandidate] = []
        unresolved: list[str] = []
        ambiguous: dict[str, list[DistrictCandidate]] = {}
        for name in names:
            normalized_name = normalize_district_name_for_resolution(name)
            matches = index.get(normalized_name, [])
            if len(matches) == 1:
                resolved.append(matches[0])
            elif len(matches) > 1:
                ambiguous[name] = matches
            else:
                unresolved.append(name)

        return DistrictResolution(
            resolved=resolved,
            unresolved=unresolved,
            ambiguous=ambiguous,
        )

    async def list_covered_districts(
        self,
        *,
        states: set[str] | None = None,
    ) -> list[DistrictCandidate]:
        """List covered districts from the configured Compass schema."""

        state_values = sorted(state.upper() for state in states or set())
        if state_values:
            sql = f"""
                SELECT
                    district_id,
                    name AS district_name,
                    state_abbrev AS state
                FROM {self._table("navigator_districts")}
                WHERE UPPER(state_abbrev) = ANY($1::text[])
                ORDER BY name, district_id
            """
            params: tuple[object, ...] = (state_values,)
        else:
            sql = f"""
                SELECT
                    district_id,
                    name AS district_name,
                    state_abbrev AS state
                FROM {self._table("navigator_districts")}
                ORDER BY name, district_id
            """
            params = ()

        async with self._acquire() as conn:
            rows = await conn.fetch(sql, *params)

        seen: set[int] = set()
        districts: list[DistrictCandidate] = []
        for row in rows:
            candidate = DistrictCandidate(**dict(row))
            if candidate.district_id in seen:
                continue
            seen.add(candidate.district_id)
            districts.append(candidate)
        return districts

    async def search_district_candidates(
        self,
        query: str,
        *,
        states: set[str] | None = None,
        limit: int = 8,
    ) -> list[DistrictCandidate]:
        """Search covered district profile rows for candidate adjudication."""

        state_values = sorted(state.upper() for state in states or set())
        tokens = _district_search_tokens(query)
        sql = f"""
            WITH candidate_districts AS (
                SELECT
                    district_id,
                    district_name,
                    state,
                    city,
                    county_name,
                    enrollment,
                    locale_type,
                    LOWER(
                        district_name || ' ' ||
                        COALESCE(state, '') || ' ' ||
                        COALESCE(state_name, '') || ' ' ||
                        COALESCE(city, '') || ' ' ||
                        COALESCE(county_name, '')
                    ) AS search_text
                FROM {self._table("district_profiles")}
                WHERE (
                    CARDINALITY($3::text[]) = 0
                    OR UPPER(state) = ANY($3::text[])
                )
            )
            SELECT
                district_id,
                district_name,
                state,
                city,
                county_name,
                enrollment,
                locale_type,
                CASE
                    WHEN LOWER(district_name) = LOWER($1) THEN 'exact_name'
                    WHEN LOWER(city) = LOWER($1) THEN 'exact_city'
                    WHEN LOWER(district_name) LIKE '%' || LOWER($1) || '%'
                        THEN 'name_contains'
                    WHEN LOWER(city) LIKE '%' || LOWER($1) || '%'
                        THEN 'city_contains'
                    ELSE 'token_search'
                END AS match_reason
            FROM candidate_districts
            WHERE LOWER(district_name) LIKE '%' || LOWER($1) || '%'
               OR LOWER(COALESCE(city, '')) LIKE '%' || LOWER($1) || '%'
               OR (
                    CARDINALITY($2::text[]) > 0
                    AND NOT EXISTS (
                        SELECT 1
                        FROM UNNEST($2::text[]) AS query_token(token)
                        WHERE search_text NOT LIKE '%' || query_token.token || '%'
                    )
               )
            ORDER BY
                CASE
                    WHEN LOWER(district_name) = LOWER($1) THEN 0
                    WHEN LOWER(city) = LOWER($1) THEN 1
                    WHEN LOWER(district_name) LIKE '%' || LOWER($1) || '%' THEN 2
                    WHEN LOWER(city) LIKE '%' || LOWER($1) || '%' THEN 3
                    ELSE 4
                END,
                district_name ASC,
                district_id ASC
            LIMIT $4
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, query, tokens, state_values, limit)
        return [DistrictCandidate(**dict(row)) for row in rows]

    async def search_nces_districts_by_name(
        self,
        name: str,
        *,
        enrollment_floor: int = 1500,
        state_cap: int = 6,
    ) -> list[NcesDistrictMatch]:
        """Return the prominent NCES district per state matching an ambiguous name.

        Unlike every other district search (which hits ``district_profiles``,
        the covered 133), this searches the FULL ``nces_districts`` universe so
        a same-name ambiguity with NO covered match can still be disambiguated
        from real rows. Joins coverage from ``district_profiles``.

        Match strategy (the feature, validated against live data): key on the
        distinctive name token (``_distinctive_district_token`` → "cleveland"),
        not the full phrase — a "%cleveland county%" substring misses "Cleveland
        Municipal" (OH). Take the top district per state by enrollment above
        ``enrollment_floor`` (suppresses charter / elementary noise — the bare
        token matches dozens of tiny rows), and cap at ``state_cap`` states by
        enrollment so the list stays legible. Returns ``[]`` when no token is
        usable.
        """

        token = _distinctive_district_token(name)
        if not token:
            return []
        sql = f"""
            WITH matches AS (
                SELECT
                    nces.leaid,
                    nces.district_name,
                    nces.state,
                    nces.enrollment,
                    (profile.nces_id IS NOT NULL) AS covered,
                    ROW_NUMBER() OVER (
                        PARTITION BY nces.state
                        ORDER BY nces.enrollment DESC NULLS LAST
                    ) AS rn_in_state
                FROM {self._table("nces_districts")} nces
                LEFT JOIN {self._table("district_profiles")} profile
                    ON profile.nces_id = nces.leaid
                WHERE LOWER(nces.district_name) LIKE '%' || $1 || '%'
                  AND COALESCE(nces.enrollment, 0) >= $2
            )
            SELECT leaid, district_name, state, enrollment, covered
            FROM matches
            WHERE rn_in_state = 1
            ORDER BY enrollment DESC NULLS LAST
            LIMIT $3
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, token, enrollment_floor, state_cap)
        return [NcesDistrictMatch(**dict(row)) for row in rows]

    async def select_largest_districts(
        self,
        *,
        states: set[str] | None = None,
        limit: int = 5,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        """Select covered districts by current-year navigator enrollment."""

        state_values = sorted(state.upper() for state in states or set())
        table = self._table("navigator_districts")
        auth_table = self._table("district_enrollment_authority")
        # Source district identity from the full roster, preferring the
        # requested academic year's row but NOT requiring it: a covered
        # district that only appears in a prior directory snapshot (no
        # current-year row) but still carries enrollment authority must
        # remain eligible for the largest set, not be silently dropped.
        # See #1022 / #1219 and scenario SORT-MIGRATED-283.
        sql = f"""
            WITH current_districts AS (
                SELECT DISTINCT ON (district_id)
                    district_id,
                    name AS district_name,
                    state_abbrev AS state
                FROM {table}
                WHERE (
                    CARDINALITY($2::text[]) = 0
                    OR UPPER(state_abbrev) = ANY($2::text[])
                  )
                ORDER BY
                    district_id,
                    (ay_range = $1) DESC,
                    ay_range DESC,
                    name ASC
            ),
            enrollment_authority AS (
                -- #1270: COALESCE the supervisory-union override onto TCD
                -- enrollment. NOTE: a union district with NO non-null TCD
                -- enrollment for any year is absent from `nav` and would not
                -- receive its authority value (NYC is covered; a FULL JOIN is
                -- the follow-up if a union district ever lacks a TCD row).
                SELECT
                    nav.district_id,
                    COALESCE(auth.enrollment, nav.enrollment) AS enrollment
                FROM (
                    SELECT DISTINCT ON (district_id)
                        district_id,
                        enrollment
                    FROM {table}
                    WHERE enrollment IS NOT NULL
                    ORDER BY district_id, ay_range DESC
                ) nav
                LEFT JOIN {auth_table} auth
                    ON auth.district_id = nav.district_id
                   AND auth.method = 'supervisory_union_rollup'
            )
            SELECT
                current_districts.district_id,
                current_districts.district_name,
                current_districts.state
            FROM current_districts
            LEFT JOIN enrollment_authority USING (district_id)
            ORDER BY
                enrollment_authority.enrollment DESC NULLS LAST,
                current_districts.district_name ASC,
                current_districts.district_id ASC
            LIMIT $3
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(
                sql,
                academic_year,
                state_values,
                limit,
            )
        return [DistrictCandidate(**dict(row)) for row in rows]

    async def select_districts_by_enrollment_range(
        self,
        *,
        states: set[str] | None = None,
        min_enrollment: int | None = None,
        max_enrollment: int | None = None,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        """Select covered districts by reviewed enrollment filter authority."""

        state_values = sorted(state.upper() for state in states or set())
        table = self._table("navigator_districts")
        auth_table = self._table("district_enrollment_authority")
        sql = f"""
            WITH current_districts AS (
                SELECT DISTINCT ON (district_id)
                    district_id,
                    name AS district_name,
                    state_abbrev AS state
                FROM {table}
                WHERE ay_range = $1
                  AND (
                    CARDINALITY($2::text[]) = 0
                    OR UPPER(state_abbrev) = ANY($2::text[])
                  )
                ORDER BY district_id, name ASC
            ),
            enrollment_authority AS (
                -- #1270: COALESCE the supervisory-union override onto TCD
                -- enrollment. NOTE: a union district with NO non-null TCD
                -- enrollment for any year is absent from `nav` and would not
                -- receive its authority value (NYC is covered; a FULL JOIN is
                -- the follow-up if a union district ever lacks a TCD row).
                SELECT
                    nav.district_id,
                    COALESCE(auth.enrollment, nav.enrollment) AS enrollment
                FROM (
                    SELECT DISTINCT ON (district_id)
                        district_id,
                        enrollment
                    FROM {table}
                    WHERE enrollment IS NOT NULL
                    ORDER BY district_id, ay_range DESC
                ) nav
                LEFT JOIN {auth_table} auth
                    ON auth.district_id = nav.district_id
                   AND auth.method = 'supervisory_union_rollup'
            )
            SELECT
                current_districts.district_id,
                current_districts.district_name,
                current_districts.state
            FROM current_districts
            JOIN enrollment_authority USING (district_id)
            WHERE ($3::integer IS NULL OR enrollment_authority.enrollment >= $3)
              AND ($4::integer IS NULL OR enrollment_authority.enrollment <= $4)
            ORDER BY
                current_districts.district_name ASC,
                current_districts.district_id ASC
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(
                sql,
                academic_year,
                state_values,
                min_enrollment,
                max_enrollment,
            )
        return [DistrictCandidate(**dict(row)) for row in rows]

    async def rank_districts_by_profile_field(
        self,
        field_key: str,
        *,
        limit: int | None = None,
        direction: str = "desc",
        states: set[str] | None = None,
        academic_year: str,
    ) -> list[dict[str, object]]:
        """Rank covered districts by a governed navigator profile field."""

        spec = _PROFILE_RANK_FIELDS.get(field_key)
        if spec is None:
            raise UnsupportedRankField(field_key=field_key)
        state_values = sorted(state.upper() for state in states or set())
        column = quote_ident(spec["column"])
        order_direction = "ASC" if direction == "asc" else "DESC"
        table = self._table("navigator_districts")
        # #1270: enrollment carries a supervisory-union roll-up override in
        # district_enrollment_authority. When ranking by enrollment, COALESCE
        # that authority onto the raw TCD value so a union district (NYC) ranks
        # on its rolled-up citywide enrollment — the same value
        # select_largest_districts and district_profiles already use. Without
        # this, "the largest districts by enrollment" normalizes into this
        # profile-ranking path (planning/profile_fields.py drops the
        # largest_districts scope) and silently bypassed the override, so NYC
        # dropped out of the top 10 (#1022 / #1219). Other governed rank fields
        # (e.g. FRPL %) have no such override and rank on the raw value. Caveat
        # mirrors select_largest_districts: a union district with no non-null
        # TCD value for any year is absent from `nav` and would not receive its
        # authority value (NYC is covered).
        if spec["column"] == "enrollment":
            auth_table = self._table("district_enrollment_authority")
            value_expr = "COALESCE(auth.enrollment, nav.value)::float"
            auth_join = f"""
                    LEFT JOIN {auth_table} auth
                        ON auth.district_id = nav.district_id
                       AND auth.method = 'supervisory_union_rollup'"""
        else:
            value_expr = "nav.value::float"
            auth_join = ""
        sql = f"""
            WITH current_districts AS (
                SELECT DISTINCT ON (district_id)
                    district_id,
                    name AS district_name,
                    state_abbrev AS state
                FROM {table}
                WHERE ay_range = $1
                  AND (
                    CARDINALITY($2::text[]) = 0
                    OR UPPER(state_abbrev) = ANY($2::text[])
                  )
                ORDER BY district_id, name ASC
            ),
            profile_authority AS (
                SELECT
                    nav.district_id,
                    {value_expr} AS value,
                    nav.release_yr
                FROM (
                    SELECT DISTINCT ON (district_id)
                        district_id,
                        {column} AS value,
                        release_yr
                    FROM {table}
                    WHERE {column} IS NOT NULL
                    ORDER BY district_id, ay_range DESC
                ) nav{auth_join}
            )
            SELECT
                current_districts.district_id,
                current_districts.district_name,
                current_districts.state,
                profile_authority.value,
                profile_authority.release_yr
            FROM current_districts
            JOIN profile_authority USING (district_id)
            ORDER BY
                profile_authority.value {order_direction} NULLS LAST,
                current_districts.district_name ASC,
                current_districts.district_id ASC
        """
        params: list[object] = [academic_year, state_values]
        if limit is not None:
            sql += "\n            LIMIT $3"
            params.append(limit)

        async with self._acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [
            {
                "district_id": row["district_id"],
                "district_name": row["district_name"],
                "state": row["state"],
                "field_key": field_key,
                "label": spec["label"],
                "value": float(row["value"]),
                "display_value": _profile_display_value(row["value"], spec["display"]),
                "academic_year": academic_year,
                "vintage": (
                    f"{spec['vintage_label']}: {row['release_yr']}"
                    if row["release_yr"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    async def lookup_nces_profile_values(
        self,
        district_names: list[str],
        *,
        field_key: str,
        states: set[str] | None = None,
        academic_year: str,
    ) -> list[ProfileFieldValueRow]:
        """Look up approved NCES profile values for covered or NCES-only districts."""

        field = _NCES_FIELD_BY_KEY.get(field_key)
        if field is None:
            return []
        display = _NCES_FIELD_DISPLAY.get(field_key)
        if display is None:
            return []

        state_values = sorted(state.upper() for state in states or set())
        column = quote_ident(field_key)
        if field_key in _NAVIGATOR_PROFILE_FIELDS:
            # Source the value from navigator_districts via the district_id link
            # (the field is not an nces_districts column). DISTINCT ON keeps the
            # most recent vintage per district; ay_range carries the true vintage
            # so provenance cites the field's year, not the NCES directory year.
            navigator_cte = f""",
            navigator_profile AS (
                SELECT DISTINCT ON (district_id)
                    district_id,
                    {column} AS profile_value,
                    ay_range AS navigator_vintage
                FROM {self._table("navigator_districts")}
                WHERE {column} IS NOT NULL
                ORDER BY district_id, ay_id DESC
            )"""
            value_select = (
                "np.profile_value AS profile_value, "
                "np.navigator_vintage AS navigator_vintage"
            )
            value_join = (
                "LEFT JOIN navigator_profile np "
                "ON np.district_id = link.district_id"
            )
            value_not_null = "np.profile_value IS NOT NULL"
        else:
            navigator_cte = ""
            value_select = f"nces.{column} AS profile_value, NULL AS navigator_vintage"
            value_join = ""
            value_not_null = f"nces.{column} IS NOT NULL"
        sql = f"""
            WITH current_covered AS (
                SELECT DISTINCT ON (district_id)
                    district_id,
                    name AS covered_district_name,
                    state_abbrev AS covered_state
                FROM {self._table("navigator_districts")}
                WHERE ay_range = $1
                ORDER BY district_id, name ASC
            ){navigator_cte}
            SELECT
                nces.leaid,
                nces.district_name AS nces_district_name,
                nces.state AS nces_state,
                {value_select},
                nces.directory_year,
                nces.finance_year,
                link.district_id,
                covered.covered_district_name,
                covered.covered_state
            FROM {self._table("nces_districts")} nces
            LEFT JOIN {self._table("navigator_nces_link")} link
              ON link.leaid = nces.leaid
             AND link.leaid != '0000000'
            LEFT JOIN current_covered covered
              ON covered.district_id = link.district_id
            {value_join}
            WHERE {value_not_null}
              AND (
                CARDINALITY($2::text[]) = 0
                OR UPPER(nces.state) = ANY($2::text[])
              )
            ORDER BY nces.district_name ASC, nces.state ASC, nces.leaid ASC
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, academic_year, state_values)

        by_name: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            data = dict(row)
            nces_key = normalize_district_name_for_resolution(
                str(data["nces_district_name"])
            )
            if nces_key:
                by_name.setdefault(nces_key, []).append(data)
            covered_name = data.get("covered_district_name")
            if isinstance(covered_name, str):
                covered_key = normalize_district_name_for_resolution(covered_name)
                if covered_key:
                    by_name.setdefault(covered_key, []).append(data)

        resolved: list[ProfileFieldValueRow] = []
        for requested_name in district_names:
            name_key = normalize_district_name_for_resolution(requested_name)
            matches = _dedupe_nces_rows(by_name.get(name_key, []))
            if len(matches) != 1:
                continue
            data = matches[0]
            raw_value = _python_value(data["profile_value"])
            resolved.append(
                ProfileFieldValueRow(
                    requested_name=requested_name,
                    district_id=data.get("district_id"),
                    district_name=(
                        data.get("covered_district_name")
                        or data["nces_district_name"]
                    ),
                    state=data.get("covered_state") or data["nces_state"],
                    field_key=field.field_key,
                    label=field.label,
                    value=raw_value,
                    display_value=_profile_display_value(raw_value, display),
                    academic_year=academic_year,
                    source=f"{self._settings.pg_schema}.nces_districts",
                    source_label="NCES district profile",
                    provenance=_nces_profile_provenance(
                        field.field_key,
                        data.get("directory_year"),
                        data.get("finance_year"),
                        navigator_vintage=data.get("navigator_vintage"),
                    ),
                    covered_by_compass=data.get("district_id") is not None,
                )
            )
        return resolved

    async def list_covered_nces_profiles(
        self,
        *,
        academic_year: str,
    ) -> list[CoveredNCESProfileRow]:
        """Return covered district NCES profile rows for deterministic peer scoring."""

        sql = f"""
            WITH current_covered AS (
                SELECT DISTINCT ON (district_id)
                    district_id,
                    name AS district_name,
                    state_abbrev AS state
                FROM {self._table("navigator_districts")}
                WHERE ay_range = $1
                ORDER BY district_id, name ASC
            ),
            frpl_authority AS (
                SELECT DISTINCT ON (district_id)
                    district_id,
                    frpl_pct
                FROM {self._table("navigator_districts")}
                WHERE frpl_pct IS NOT NULL
                ORDER BY district_id, ay_id DESC
            )
            SELECT
                covered.district_id,
                covered.district_name,
                covered.state,
                nces.city,
                nces.enrollment,
                nces.locale_text,
                nces.latitude::float AS latitude,
                nces.longitude::float AS longitude,
                nces.total_rev_pp::float AS total_rev_pp,
                nces.total_exp_pp::float AS total_exp_pp,
                nces.pupil_teacher_ratio::float AS pupil_teacher_ratio,
                frpl.frpl_pct::float AS frpl_pct,
                nces.directory_year,
                nces.finance_year
            FROM current_covered covered
            JOIN {self._table("navigator_nces_link")} link
              ON link.district_id = covered.district_id
             AND link.leaid != '0000000'
            JOIN {self._table("nces_districts")} nces
              ON nces.leaid = link.leaid
            LEFT JOIN frpl_authority frpl
              ON frpl.district_id = covered.district_id
            ORDER BY covered.district_name ASC, covered.district_id ASC
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, academic_year)
        return [
            CoveredNCESProfileRow(**{key: _python_value(value) for key, value in dict(row).items()})
            for row in rows
        ]

    async def search_source_documents(
        self,
        query: str,
        *,
        district_ids: set[int] | None = None,
        limit: int = 5,
    ) -> list[SourceDocumentCandidate]:
        """Search source documents through the catalog boundary."""

        district_values = sorted(district_ids or set())
        sql = f"""
            SELECT
                source_id,
                district_id,
                title,
                document_type,
                valid_year_range AS academic_year,
                source_link AS url,
                is_download
            FROM {self._table("navigator_sources")}
            WHERE (
                LOWER(title) LIKE '%' || LOWER($1) || '%'
                OR LOWER(COALESCE(document_type, '')) LIKE '%' || LOWER($1) || '%'
            )
              AND (CARDINALITY($2::int[]) = 0 OR district_id = ANY($2::int[]))
            ORDER BY
                CASE
                    WHEN LOWER(title) = LOWER($1) THEN 0
                    WHEN LOWER(title) LIKE LOWER($1) || '%' THEN 1
                    WHEN LOWER(title) LIKE '%' || LOWER($1) || '%' THEN 2
                    ELSE 3
                END,
                source_id ASC
            LIMIT $3
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, query, district_values, limit)
        candidates: list[SourceDocumentCandidate] = []
        for row in rows:
            data = dict(row)
            is_download = data.pop("is_download", None)
            data["metadata"] = (
                {"is_download": is_download} if is_download is not None else {}
            )
            candidates.append(SourceDocumentCandidate(**data))
        return candidates

    async def search_topics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[TopicCandidate]:
        """Search Compass topics and subtopics."""

        sql = f"""
            SELECT
                topic_id,
                topic_name,
                subtopic_id,
                subtopic_name,
                question_count
            FROM {self._table("navigator_topics")}
            WHERE LOWER(topic_name) LIKE '%' || LOWER($1) || '%'
               OR LOWER(COALESCE(subtopic_name, '')) LIKE '%' || LOWER($1) || '%'
            ORDER BY
                CASE
                    WHEN LOWER(topic_name) = LOWER($1) THEN 0
                    WHEN LOWER(COALESCE(subtopic_name, '')) = LOWER($1) THEN 1
                    ELSE 2
                END,
                display_order ASC,
                topic_id ASC,
                subtopic_id ASC
            LIMIT $2
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, query, limit)
        return [TopicCandidate(**dict(row)) for row in rows]

    async def fetch_topic_metric_candidates(
        self,
        topics: list[TopicCandidate],
        *,
        limit: int = 10,
    ) -> list[MetricCandidate]:
        """Fetch deterministic metric candidates linked to topic candidates."""

        topic_names = _unique_text_values(topic.topic_name for topic in topics)
        subtopic_names = _unique_text_values(
            topic.subtopic_name for topic in topics if topic.subtopic_name
        )
        if not topic_names and not subtopic_names:
            return []
        sql = f"""
            SELECT
                metric.metric_id,
                metric.name,
                metric.topic,
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM {self._table("navigator_answers")} answer
                        WHERE answer.metric_id = metric.metric_id
                          AND answer.value ~
                              '^[[:space:]]*[$]?[[:space:]]*-?[0-9][0-9,]*([.][0-9]+)?[[:space:]]*%?[[:space:]]*$'
                    )
                    THEN 'numeric'
                    ELSE metric.answer_type
                END AS answer_type
            FROM {self._table("navigator_metrics")} metric
            WHERE metric.topic = ANY($1::text[])
               OR COALESCE(metric.subtopic, '') = ANY($2::text[])
            ORDER BY
                CASE
                    WHEN COALESCE(metric.subtopic, '') = ANY($2::text[]) THEN 0
                    WHEN metric.topic = ANY($1::text[]) THEN 1
                    ELSE 2
                END,
                metric.metric_id ASC
            LIMIT $3
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, topic_names, subtopic_names, limit)
        return [MetricCandidate(**dict(row)) for row in rows]

    async def fetch_topic_content_links(
        self,
        topics: list[TopicCandidate],
        *,
        limit: int = 10,
    ) -> list[TopicContentLink]:
        """Fetch approved internal related-content links for topic diagnostics."""

        topic_ids = _unique_int_values(topic.topic_id for topic in topics)
        subtopic_ids = _unique_int_values(
            topic.subtopic_id for topic in topics if topic.subtopic_id is not None
        )
        if not topic_ids and not subtopic_ids:
            return []
        sql = f"""
            SELECT
                link.topic_id,
                COALESCE(topic.topic_name, link.topic_id::text) AS topic_name,
                link.subtopic_id,
                topic.subtopic_name,
                link.content_type,
                link.content_id,
                link.label,
                link.source,
                link.provenance
            FROM {self._table("topic_content_links")} link
            LEFT JOIN {self._table("navigator_topics")} topic
              ON topic.subtopic_id = link.subtopic_id
            WHERE link.active IS TRUE
              AND link.review_status = 'approved'
              AND (
                    link.topic_id = ANY($1::int[])
                    OR link.subtopic_id = ANY($2::int[])
              )
            ORDER BY
                link.content_type,
                link.topic_id,
                link.subtopic_id NULLS LAST,
                link.content_id
            LIMIT $3
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, topic_ids, subtopic_ids, limit)
        return [TopicContentLink(**dict(row)) for row in rows]

    async def search_glossary_terms(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[GlossaryTermCandidate]:
        """Search glossary terms."""

        sql = f"""
            SELECT
                LOWER(term) AS term_id,
                term,
                definition,
                context
            FROM {self._table("glossary")}
            WHERE LOWER(term) LIKE '%' || LOWER($1) || '%'
               OR LOWER(COALESCE(definition, '')) LIKE '%' || LOWER($1) || '%'
               OR LOWER(COALESCE(context, '')) LIKE '%' || LOWER($1) || '%'
            ORDER BY
                CASE
                    WHEN LOWER(term) = LOWER($1) THEN 0
                    WHEN LOWER(term) LIKE LOWER($1) || '%' THEN 1
                    WHEN LOWER(term) LIKE '%' || LOWER($1) || '%' THEN 2
                    ELSE 3
                END,
                term ASC
            LIMIT $2
        """
        async with self._acquire() as conn:
            rows = await conn.fetch(sql, query, limit)
        return [GlossaryTermCandidate(**dict(row)) for row in rows]

    async def search_nces_fields(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[NCESFieldCandidate]:
        """Search approved NCES field keys present in the configured schema."""

        normalized = query.casefold().strip().replace("_", " ")
        tokens = _profile_field_tokens(query)
        candidates = [
            field
            for field in _NCES_FIELDS
            if normalized in field.field_key.casefold().replace("_", " ")
            or normalized in field.label.casefold()
            or (
                field.description is not None
                and normalized in field.description.casefold()
            )
            or _profile_field_token_match(tokens, field)
        ]
        if not candidates:
            return []

        candidates.sort(
            key=lambda field: (
                0
                if field.field_key.casefold().replace("_", " ") == normalized
                else 1,
                0 if field.label.casefold() == normalized else 1,
                -_profile_field_match_count(tokens, field),
                field.field_key,
            )
        )
        approved_keys = {field.field_key for field in candidates}
        all_columns = await self._ensure_nces_fields_cache(self._settings.pg_schema)
        available_keys = approved_keys & all_columns
        # FRPL is a governed profile rank field sourced from navigator_districts,
        # not a physical nces_districts column.
        available_keys.add("frpl_pct")
        return [
            field for field in candidates if field.field_key in available_keys
        ][:limit]

    async def _ensure_nces_fields_cache(self, schema: str) -> frozenset[str]:
        """Return the cached set of column names for ``nces_districts``.

        Loads all column names for ``(schema, 'nces_districts')`` from
        ``information_schema.columns`` once per process lifetime, keyed by
        ``(schema, table)``.  Subsequent calls for the same key return the
        cached ``frozenset`` without touching the pool.

        Use ``reset_nces_fields_cache()`` to invalidate (tests, or after a
        live schema migration without restarting the process).
        """
        key = (schema, "nces_districts")
        cached = _nces_fields_cache.get(key)
        if cached is not None:
            return cached
        async with _nces_fields_cache_lock:
            # Re-check after acquiring the lock — another coroutine may have
            # loaded the cache while we were waiting.
            cached = _nces_fields_cache.get(key)
            if cached is not None:
                return cached
            sql = """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = $1
                  AND table_name = 'nces_districts'
            """
            async with self._acquire() as conn:
                rows = await conn.fetch(sql, schema)
            result: frozenset[str] = frozenset(str(row["column_name"]) for row in rows)
            _nces_fields_cache[key] = result
            return result


def _profile_display_value(value: object, display: object) -> str:
    if value is None:
        return "Not available"
    if display == "text":
        return str(value)
    numeric = float(value)
    if display == "integer":
        return f"{int(round(numeric)):,}"
    if display == "percent":
        return f"{numeric:g}%"
    if display == "currency":
        return f"${numeric:,.0f}"
    return f"{numeric:g}"


def _python_value(value: object) -> str | int | float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, int | float | str):
        return value
    return str(value)


def _nces_profile_provenance(
    field_key: str,
    directory_year: object,
    finance_year: object,
    *,
    navigator_vintage: object = None,
) -> str:
    # Navigator-sourced fields (e.g. frpl_pct) carry their own vintage; cite it
    # rather than the NCES directory year, which is a different table's year.
    if field_key in _NAVIGATOR_PROFILE_FIELDS and navigator_vintage:
        return f"NCES profile year: {navigator_vintage}"
    if field_key in {"total_rev_pp", "total_exp_pp"} and finance_year is not None:
        return f"NCES finance year: {finance_year}"
    if directory_year is not None:
        return f"NCES directory year: {directory_year}"
    return "NCES profile vintage unavailable"


def _dedupe_nces_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[object] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        key = row.get("leaid") or (
            row.get("nces_district_name"),
            row.get("nces_state"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _profile_field_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    for raw_token in _PROFILE_TOKEN_RE.sub(" ", query.casefold()).split():
        if len(raw_token) < 3 or raw_token in _PROFILE_FIELD_STOPWORDS:
            continue
        if raw_token not in tokens:
            tokens.append(raw_token)
    return tokens


def _profile_field_token_match(
    tokens: list[str],
    field: NCESFieldCandidate,
) -> bool:
    return _profile_field_match_count(tokens, field) > 0


def _profile_field_match_count(
    tokens: list[str],
    field: NCESFieldCandidate,
) -> int:
    if not tokens:
        return 0
    haystack = " ".join(
        value
        for value in [
            field.field_key.casefold().replace("_", " "),
            field.label.casefold(),
            (field.description or "").casefold(),
        ]
        if value
    )
    return sum(1 for token in tokens if token in haystack)


def _metric_search_tokens(query: str) -> list[str]:
    """Return deterministic lexical metric-search tokens."""

    tokens: list[str] = []
    for raw_token in _METRIC_TOKEN_RE.sub(" ", query.casefold()).split():
        if len(raw_token) < 3 or raw_token in _METRIC_STOPWORDS:
            continue
        if raw_token not in tokens:
            tokens.append(raw_token)
    return tokens


def _district_search_tokens(query: str) -> list[str]:
    """Return deterministic lexical district-search tokens."""

    tokens: list[str] = []
    for raw_token in _METRIC_TOKEN_RE.sub(" ", query.casefold()).split():
        if len(raw_token) < 2 or raw_token in _DISTRICT_STOPWORDS:
            continue
        if raw_token not in tokens:
            tokens.append(raw_token)
    return tokens


# Generic district-type words that follow the proper-noun name (e.g. "Cleveland
# County", "York City", "Springfield ISD"). Dropped when picking the single
# distinctive name token so the NCES disambiguation search keys on the proper
# noun ("cleveland"), finding every match — including "Cleveland Municipal" —
# rather than only the full-phrase substring "%cleveland county%".
_DISTRICT_TYPE_WORDS = frozenset(
    {
        "county",
        "city",
        "municipal",
        "unified",
        "independent",
        "consolidated",
        "community",
        "township",
        "borough",
        "parish",
        "central",
        "local",
        "area",
        "joint",
        "isd",
        "usd",
        "csd",
        "metropolitan",
    }
)


def _distinctive_district_token(name: str) -> str | None:
    """Return the single most distinctive lexical token of a district name.

    The longest token after dropping generic district-type words ("County",
    "City", "ISD", …) and the existing district stopwords. For "Cleveland
    County" → "cleveland"; for "Springfield Public Schools" → "springfield".
    Falls back to the longest plain token when every token is generic, and to
    None when nothing usable remains.
    """

    tokens = _district_search_tokens(name)
    if not tokens:
        return None
    content = [token for token in tokens if token not in _DISTRICT_TYPE_WORDS]
    return max(content or tokens, key=len)


def _unique_text_values(values: Iterable[str | None]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if not value or value in unique:
            continue
        unique.append(value)
    return unique


def _unique_int_values(values: Iterable[int]) -> list[int]:
    unique: list[int] = []
    for value in values:
        if value in unique:
            continue
        unique.append(value)
    return unique


def _catalog_alias_lookup_values(alias: str) -> list[str]:
    """Return generic and district-normalized alias lookup keys."""

    generic = _METRIC_TOKEN_RE.sub(" ", alias.casefold()).strip()
    generic = re.sub(r"\s+", " ", generic)
    district = normalize_district_name_for_resolution(alias)
    values = {value for value in (generic, district) if value}
    return sorted(values)


def _jsonb_list(value: Any) -> list[Any]:
    """Return JSONB array values as Python lists across asyncpg codecs."""

    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    return list(value)


def _jsonb_object(value: Any) -> dict[str, Any]:
    """Return JSONB object values as Python dicts across asyncpg codecs."""

    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return dict(value)
