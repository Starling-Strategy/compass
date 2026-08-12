"""Tests for degree_lane typed field on MetricSpec (PR 2C).

TDD sequence (from pr-2c-plan.md):
  Step 1 — contract: MetricSpec accepts degree_lane, default None
  Step 2 — catalog helpers: _candidate_lane, _filter_candidates_by_degree_lane
  Step 3 — catalog API: resolve_metric_bundle with degree_lane plumbing
  Step 4 — executor scoping: _resolve_plan_metrics passes lane to catalog
  Step 5 — executor operations: rank/_execute_rank passes lane, FRPL guard
  Step 6 — PR 2A composition: _resolve_metric_value_filters inherits lane from MetricSpec
  Step 7 — planner shape tests: degree_lane=None for bare phrases, degree_lane="ma"/"ba" for qualified

All tests were written BEFORE implementation (TDD). They fail initially and pass
after the implementation lands.
"""

from __future__ import annotations

import pytest

from compass_backend.catalog import (
    CatalogAliasRecord,
    DistrictCandidate,
    DistrictResolution,
    MetricCandidate,
    normalize_district_name_for_resolution,
)
from compass_backend.catalog.resolution import NCESFieldCandidate, RendererNote
from compass_backend.contracts import FilterSpec, MetricSpec, ProfileFieldSpec, QueryPlan, SelectionSpec, SortSpec
from compass_backend.execution import DeterministicQueryExecutor, MetricAnswerRow
from compass_backend.execution.types import ExecutionClarification, ExecutionRefusal


# ---------------------------------------------------------------------------
# Step 1 — Contract: MetricSpec accepts degree_lane
# ---------------------------------------------------------------------------


def test_metric_spec_accepts_degree_lane_none() -> None:
    """MetricSpec with no degree_lane defaults to None."""
    spec = MetricSpec(name="starting salary")
    assert spec.degree_lane is None


def test_metric_spec_accepts_degree_lane_ma() -> None:
    """MetricSpec accepts degree_lane='ma'."""
    spec = MetricSpec(name="starting salary", degree_lane="ma")
    assert spec.degree_lane == "ma"


def test_metric_spec_accepts_degree_lane_ba() -> None:
    """MetricSpec accepts degree_lane='ba'."""
    spec = MetricSpec(name="starting salary", degree_lane="ba")
    assert spec.degree_lane == "ba"


def test_metric_spec_rejects_invalid_degree_lane() -> None:
    """MetricSpec rejects degree_lane values outside Literal['ba', 'ma']."""
    import pydantic

    with pytest.raises((pydantic.ValidationError, ValueError)):
        MetricSpec(name="starting salary", degree_lane="any")  # type: ignore[arg-type]

    with pytest.raises((pydantic.ValidationError, ValueError)):
        MetricSpec(name="starting salary", degree_lane="phd")  # type: ignore[arg-type]


def test_metric_spec_json_roundtrip_with_degree_lane() -> None:
    """MetricSpec serialises/deserialises degree_lane correctly."""
    spec = MetricSpec(name="master's degree starting salary", degree_lane="ma")
    json_str = spec.model_dump_json()
    restored = MetricSpec.model_validate_json(json_str)
    assert restored.degree_lane == "ma"
    assert restored.name == "master's degree starting salary"


def test_metric_spec_json_roundtrip_null_degree_lane() -> None:
    """Existing JSON without degree_lane is accepted (backward-compat)."""
    json_str = '{"name": "starting salary", "role": "primary"}'
    spec = MetricSpec.model_validate_json(json_str)
    assert spec.degree_lane is None


# ---------------------------------------------------------------------------
# Step 2 — Catalog helpers: _candidate_lane, _filter_candidates_by_degree_lane
# ---------------------------------------------------------------------------


def _make_ba_metric() -> MetricCandidate:
    return MetricCandidate(
        metric_id=89,
        name="Annual base salary for a first year teacher with a bachelor's degree",
        answer_type="numeric",
    )


def _make_ma_metric() -> MetricCandidate:
    return MetricCandidate(
        metric_id=96,
        name="Annual base salary for a first year teacher with a master's degree",
        answer_type="numeric",
    )


def _make_unrelated_metric() -> MetricCandidate:
    return MetricCandidate(
        metric_id=69,
        name="Teacher contracted workdays per year",
        answer_type="numeric",
    )


def test_candidate_lane_ba() -> None:
    """_candidate_lane returns 'ba' for a bachelor's-degree metric name."""
    from compass_backend.catalog.resolution import _candidate_lane  # noqa: PLC0415

    result = _candidate_lane(_make_ba_metric())
    assert result == "ba"


def test_candidate_lane_ma() -> None:
    """_candidate_lane returns 'ma' for a master's-degree metric name."""
    from compass_backend.catalog.resolution import _candidate_lane  # noqa: PLC0415

    result = _candidate_lane(_make_ma_metric())
    assert result == "ma"


def test_candidate_lane_none_for_unrelated() -> None:
    """_candidate_lane returns None for a metric with no degree qualifier."""
    from compass_backend.catalog.resolution import _candidate_lane  # noqa: PLC0415

    result = _candidate_lane(_make_unrelated_metric())
    assert result is None


def test_filter_candidates_by_degree_lane_keeps_ma() -> None:
    """_filter_candidates_by_degree_lane('ma') retains only MA-lane candidates."""
    from compass_backend.catalog.resolution import (  # noqa: PLC0415
        _filter_candidates_by_degree_lane,
    )

    candidates = [_make_ba_metric(), _make_ma_metric(), _make_unrelated_metric()]
    result = _filter_candidates_by_degree_lane(candidates, "ma")
    ids = {c.metric_id for c in result}
    assert 96 in ids, "MA metric (96) should survive the 'ma' filter"
    assert 89 not in ids, "BA metric (89) should be excluded by 'ma' filter"
    # Unrelated metrics (no degree lane) are included so they don't disappear silently.
    assert 69 in ids, "Lane-neutral metrics should pass through unchanged"


def test_filter_candidates_by_degree_lane_keeps_ba() -> None:
    """_filter_candidates_by_degree_lane('ba') retains only BA-lane candidates."""
    from compass_backend.catalog.resolution import (  # noqa: PLC0415
        _filter_candidates_by_degree_lane,
    )

    candidates = [_make_ba_metric(), _make_ma_metric(), _make_unrelated_metric()]
    result = _filter_candidates_by_degree_lane(candidates, "ba")
    ids = {c.metric_id for c in result}
    assert 89 in ids, "BA metric (89) should survive the 'ba' filter"
    assert 96 not in ids, "MA metric (96) should be excluded by 'ba' filter"
    assert 69 in ids, "Lane-neutral metrics should pass through unchanged"


def test_filter_candidates_conflict_returns_empty() -> None:
    """When lane is requested but only opposite-lane candidates exist, return empty list."""
    from compass_backend.catalog.resolution import (  # noqa: PLC0415
        _filter_candidates_by_degree_lane,
    )

    # Only BA candidate — requesting MA lane → no matching results
    candidates = [_make_ba_metric()]
    result = _filter_candidates_by_degree_lane(candidates, "ma")
    assert result == [], "Conflicting lane should return empty list, not the wrong metric"


# ---------------------------------------------------------------------------
# Fake repository for executor integration tests
# ---------------------------------------------------------------------------


class FakeDegreeRepository:
    """Minimal repository with BA (metric_id=89) and MA (metric_id=96) salary metrics."""

    # BA metric_id is 89, MA metric_id is 96 (mirrors seed_catalog_aliases.sql)
    BA_METRIC_ID = 89
    MA_METRIC_ID = 96

    def __init__(
        self,
        *,
        aliases: list[CatalogAliasRecord] | None = None,
        nces_fields: list[NCESFieldCandidate] | None = None,
        profile_rank_rows: list[dict[str, object]] | None = None,
        renderer_notes: list[RendererNote] | None = None,
    ) -> None:
        self.aliases = aliases or []
        self._nces_fields: list[NCESFieldCandidate] = nces_fields or []
        self._profile_rank_rows: list[dict[str, object]] = profile_rank_rows or []
        self._renderer_notes: list[RendererNote] = renderer_notes or []
        self._ba_metric = MetricCandidate(
            metric_id=self.BA_METRIC_ID,
            name="Annual base salary for a first year teacher with a bachelor's degree",
            answer_type="numeric",
        )
        self._ma_metric = MetricCandidate(
            metric_id=self.MA_METRIC_ID,
            name="Annual base salary for a first year teacher with a master's degree",
            answer_type="numeric",
        )
        self.metric_queries: list[str] = []
        self.fetched_metric_ids: list[int] = []

    @property
    def _all_metrics(self) -> list[MetricCandidate]:
        return [self._ba_metric, self._ma_metric]

    async def search_metrics(self, query: str, *, limit: int = 5) -> list[MetricCandidate]:
        self.metric_queries.append(query)
        normalized = query.casefold()
        return [
            m
            for m in self._all_metrics
            if any(
                token in m.name.casefold()
                for token in normalized.split()
                if len(token) > 3
            )
        ][:limit]

    async def fetch_metrics_by_ids(self, metric_ids: list[int]) -> list[MetricCandidate]:
        self.metric_queries.append("ids:" + ",".join(str(i) for i in metric_ids))
        by_id = {m.metric_id: m for m in self._all_metrics}
        return [by_id[i] for i in metric_ids if i in by_id]

    async def search_catalog_aliases(
        self, alias: str, *, entity_types: set[str]
    ) -> list[CatalogAliasRecord]:
        normalized = normalize_district_name_for_resolution(alias)
        return [
            r
            for r in self.aliases
            if r.entity_type in entity_types
            and r.active
            and r.normalized_alias == normalized
        ]

    async def list_covered_districts(
        self, *, states: set[str] | None = None
    ) -> list[DistrictCandidate]:
        return self._districts()

    def _districts(self) -> list[DistrictCandidate]:
        return [
            DistrictCandidate(district_id=1, district_name="Wake County", state="NC"),
            DistrictCandidate(district_id=2, district_name="Aldine ISD", state="TX"),
        ]

    async def resolve_districts(
        self, names: list[str], *, states: set[str] | None = None
    ) -> DistrictResolution:
        # Resolve by exact normalized name (realistic contract). The historic
        # stub returned every district as ``resolved`` for any name; that masked
        # anchor resolution, which #1363 now exercises (peer/similarity anchors
        # pre-resolve through resolve_referent_district before resolve_peer_set).
        state_filter = {state.upper() for state in states or set()}
        resolved: list[DistrictCandidate] = []
        unresolved: list[str] = []
        ambiguous: dict[str, list[DistrictCandidate]] = {}
        for name in names:
            normalized = normalize_district_name_for_resolution(name)
            candidates = [
                district
                for district in self._districts()
                if normalize_district_name_for_resolution(district.district_name)
                == normalized
                and (
                    not state_filter
                    or (district.state or "").upper() in state_filter
                )
            ]
            if len(candidates) == 1:
                resolved.append(candidates[0])
            elif len(candidates) > 1:
                ambiguous[name] = candidates
            else:
                unresolved.append(name)
        return DistrictResolution(
            resolved=resolved, unresolved=unresolved, ambiguous=ambiguous
        )

    async def select_largest_districts(
        self,
        *,
        states: set[str] | None = None,
        limit: int | None = 5,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        return self._districts()

    async def select_districts_by_enrollment_range(
        self,
        *,
        states: set[str] | None = None,
        min_enrollment: int | None = None,
        max_enrollment: int | None = None,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        return self._districts()

    async def fetch_metric_answer_rows(
        self, *, metric_id: int, academic_year: str
    ) -> list[MetricAnswerRow]:
        self.fetched_metric_ids.append(metric_id)
        rows_by_id = {
            self.BA_METRIC_ID: [
                MetricAnswerRow(
                    district_id=1, district_name="Wake County", state="NC",
                    metric_id=self.BA_METRIC_ID,
                    metric_name="Annual base salary for a first year teacher with a bachelor's degree",
                    value=47000, academic_year=academic_year,
                ),
                MetricAnswerRow(
                    district_id=2, district_name="Aldine ISD", state="TX",
                    metric_id=self.BA_METRIC_ID,
                    metric_name="Annual base salary for a first year teacher with a bachelor's degree",
                    value=45000, academic_year=academic_year,
                ),
            ],
            self.MA_METRIC_ID: [
                MetricAnswerRow(
                    district_id=1, district_name="Wake County", state="NC",
                    metric_id=self.MA_METRIC_ID,
                    metric_name="Annual base salary for a first year teacher with a master's degree",
                    value=53000, academic_year=academic_year,
                ),
                MetricAnswerRow(
                    district_id=2, district_name="Aldine ISD", state="TX",
                    metric_id=self.MA_METRIC_ID,
                    metric_name="Annual base salary for a first year teacher with a master's degree",
                    value=51000, academic_year=academic_year,
                ),
            ],
        }
        return rows_by_id.get(metric_id, [])

    async def fetch_metric_answer_rows_for_year(
        self, *, metric_id: int, academic_year: str, district_ids: set[int]
    ) -> list[MetricAnswerRow]:
        self.fetched_metric_ids.append(metric_id)
        rows = await self.fetch_metric_answer_rows(
            metric_id=metric_id, academic_year=academic_year
        )
        return [r for r in rows if r.district_id in district_ids]

    async def fetch_metric_answer_rows_for_years(
        self, *, metric_id: int, academic_years: list[str], district_ids: set[int]
    ) -> list[MetricAnswerRow]:
        return []

    async def fetch_reviewed_district_ids(
        self, *, academic_year: str, district_ids: set[int]
    ) -> set[int]:
        return district_ids

    async def fetch_recent_metric_answer_rows(
        self, *, metric_id: int, before_academic_year: str, district_ids: set[int]
    ) -> list[MetricAnswerRow]:
        return []

    async def list_available_academic_years(
        self, *, metric_id: int, district_ids: set[int]
    ) -> list[str]:
        return ["2024 - 2025"]

    async def list_covered_nces_profiles(
        self, *, academic_year: str
    ) -> list[dict[str, object]]:
        return []

    async def fetch_renderer_notes(self, note_keys: list[str]) -> list[RendererNote]:
        return [
            note
            for note in self._renderer_notes
            if note.note_key in note_keys
        ]

    async def search_nces_fields(
        self, query: str, *, limit: int = 5
    ) -> list[NCESFieldCandidate]:
        """Return NCES field candidates matching the query (for profile-sort routing)."""
        return [
            f
            for f in self._nces_fields
            if query.casefold() in f.field_key.casefold()
            or query.casefold() in f.label.casefold()
        ][:limit]

    async def rank_districts_by_profile_field(
        self,
        field_key: str,
        *,
        limit: int | None = None,
        direction: str = "desc",
        states: set[str] | None = None,
        academic_year: str,
    ) -> list[dict[str, object]]:
        """Return profile-rank rows (for _execute_profile_ordered_metric_ranking)."""
        rows = self._profile_rank_rows
        if limit is not None:
            rows = rows[:limit]
        return rows


def _ambiguous_salary_alias() -> CatalogAliasRecord:
    """starting salary → ambiguous [89, 96]."""
    return CatalogAliasRecord(
        alias="starting salary",
        normalized_alias=normalize_district_name_for_resolution("starting salary"),
        entity_type="metric_bundle",
        resolution_status="ambiguous",
        candidate_refs=[{"metric_id": 89}, {"metric_id": 96}],
        canonical_ids=["89", "96"],
        source="test",
        provenance="test fixture",
        scenario_ids=[],
        review_status="approved",
    )


# ---------------------------------------------------------------------------
# Step 3 — Catalog API: resolve_metric_bundle with degree_lane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_metric_bundle_with_ma_lane_picks_metric_96() -> None:
    """When degree_lane='ma' and the alias is ambiguous [89, 96],
    resolve_metric_bundle returns resolved=[metric_id=96]."""
    from compass_backend.catalog import CatalogResolver  # noqa: PLC0415

    repo = FakeDegreeRepository(aliases=[_ambiguous_salary_alias()])
    resolver = CatalogResolver(repo)

    result = await resolver.resolve_metric_bundle(
        "starting salary",
        degree_lane="ma",
        numeric_only=True,
    )

    assert not result.ambiguous, (
        "With degree_lane='ma', should resolve (not clarify)"
    )
    assert len(result.resolved) == 1, "Expected exactly one resolved metric"
    assert result.resolved[0].metric_id == 96, (
        f"Expected MA metric (96), got {result.resolved[0].metric_id}"
    )


@pytest.mark.asyncio
async def test_resolve_metric_bundle_with_ba_lane_picks_metric_89() -> None:
    """When degree_lane='ba' and the alias is ambiguous [89, 96],
    resolve_metric_bundle returns resolved=[metric_id=89]."""
    from compass_backend.catalog import CatalogResolver  # noqa: PLC0415

    repo = FakeDegreeRepository(aliases=[_ambiguous_salary_alias()])
    resolver = CatalogResolver(repo)

    result = await resolver.resolve_metric_bundle(
        "starting salary",
        degree_lane="ba",
        numeric_only=True,
    )

    assert not result.ambiguous, (
        "With degree_lane='ba', should resolve (not clarify)"
    )
    assert len(result.resolved) == 1, "Expected exactly one resolved metric"
    assert result.resolved[0].metric_id == 89, (
        f"Expected BA metric (89), got {result.resolved[0].metric_id}"
    )


@pytest.mark.asyncio
async def test_resolve_metric_bundle_without_lane_stays_ambiguous() -> None:
    """Without degree_lane, an ambiguous phrase still returns ambiguous=True."""
    from compass_backend.catalog import CatalogResolver  # noqa: PLC0415

    repo = FakeDegreeRepository(aliases=[_ambiguous_salary_alias()])
    resolver = CatalogResolver(repo)

    result = await resolver.resolve_metric_bundle(
        "starting salary",
        degree_lane=None,
        numeric_only=True,
    )

    assert result.ambiguous, "Without lane, ambiguous alias should remain ambiguous"


@pytest.mark.asyncio
async def test_resolve_metric_bundle_lane_conflict_returns_empty() -> None:
    """When degree_lane='ma' but only BA candidates exist after alias resolution,
    returns empty resolved list (not silently picks BA)."""
    from compass_backend.catalog import CatalogResolver  # noqa: PLC0415

    # BA-only alias
    ba_only_alias = CatalogAliasRecord(
        alias="ba starting salary",
        normalized_alias=normalize_district_name_for_resolution("ba starting salary"),
        entity_type="metric_bundle",
        resolution_status="approved",
        candidate_refs=[{"metric_id": 89}],
        canonical_ids=["89"],
        source="test",
        provenance="test fixture",
        scenario_ids=[],
        review_status="approved",
    )
    repo = FakeDegreeRepository(aliases=[ba_only_alias])
    resolver = CatalogResolver(repo)

    result = await resolver.resolve_metric_bundle(
        "ba starting salary",
        degree_lane="ma",  # contradicts the BA-only alias
        numeric_only=True,
    )

    # Should not silently pick the BA metric — empty resolved list (refusal upstream)
    assert not result.resolved, (
        "Lane conflict (BA alias + MA lane) should produce empty resolved list"
    )


# ---------------------------------------------------------------------------
# Step 4 — Executor scoping: _resolve_plan_metrics passes lane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_plan_metrics_ma_lane_resolves_metric_96() -> None:
    """A lookup plan with degree_lane='ma' must resolve to metric_id=96 rows."""

    repo = FakeDegreeRepository(aliases=[_ambiguous_salary_alias()])
    executor = DeterministicQueryExecutor(repo)

    plan = QueryPlan(
        operation="lookup",
        question="What is starting salary for teachers with a master's degree in Wake County?",
        selection=SelectionSpec(scope="named_districts", districts=["Wake County"]),
        metrics=[MetricSpec(name="starting salary", degree_lane="ma")],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    # All returned rows must be from the MA metric (96)
    returned_metric_ids = {row.metric_id for row in result.rows}
    assert 96 in returned_metric_ids, "MA lane must resolve to metric_id=96 rows"
    assert 89 not in returned_metric_ids, "BA metric (89) must not appear in MA-lane result"


@pytest.mark.asyncio
async def test_resolve_plan_metrics_ba_lane_resolves_metric_89() -> None:
    """A lookup plan with degree_lane='ba' must resolve to metric_id=89 rows."""

    repo = FakeDegreeRepository(aliases=[_ambiguous_salary_alias()])
    executor = DeterministicQueryExecutor(repo)

    plan = QueryPlan(
        operation="lookup",
        question="What is starting salary for teachers with a bachelor's degree in Wake County?",
        selection=SelectionSpec(scope="named_districts", districts=["Wake County"]),
        metrics=[MetricSpec(name="starting salary", degree_lane="ba")],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    returned_metric_ids = {row.metric_id for row in result.rows}
    assert 89 in returned_metric_ids, "BA lane must resolve to metric_id=89 rows"
    assert 96 not in returned_metric_ids, "MA metric (96) must not appear in BA-lane result"


# ---------------------------------------------------------------------------
# Step 6 — PR 2A composition: _resolve_metric_value_filters inherits lane
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_inherits_ma_lane_from_matching_metric_spec() -> None:
    """When a FilterSpec.field matches a MetricSpec.name with degree_lane='ma',
    the filter's catalog resolution must use the MA lane (metric_id=96 rows).

    Scenario: lookup with starting salary > 52000 where degree_lane='ma'.
    Only Wake County (MA salary = 53000) should pass; Aldine ISD (MA = 51000) excluded.
    """

    repo = FakeDegreeRepository(aliases=[_ambiguous_salary_alias()])
    executor = DeterministicQueryExecutor(repo)

    plan = QueryPlan(
        operation="lookup",
        question="Which districts have MA starting salary above 52000?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary", degree_lane="ma")],
        filters=[
            FilterSpec(field="starting salary", operator="greater_than", value=52000),
        ],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    district_ids = {row.district_id for row in result.rows}
    assert 1 in district_ids, "Wake County (MA salary=53000 > 52000) should pass"
    assert 2 not in district_ids, "Aldine ISD (MA salary=51000 < 52000) should be excluded"
    # Confirm MA rows were used (not BA rows which are 47000 and 45000)
    metric_ids_used = repo.fetched_metric_ids
    assert 96 in metric_ids_used, "MA metric (96) must have been fetched for MA-lane filter"
    assert 89 not in metric_ids_used, "BA metric (89) must not be fetched for MA-lane filter"


# ---------------------------------------------------------------------------
# Fix 5 (PR 2C review) — FRPL contextual default skip when degree_lane explicit
# ---------------------------------------------------------------------------


def _frpl_profile_alias() -> CatalogAliasRecord:
    """frpl_pct NCES field alias."""
    return CatalogAliasRecord(
        alias="frpl_pct",
        normalized_alias=normalize_district_name_for_resolution("frpl_pct"),
        entity_type="nces_field",
        resolution_status="approved",
        candidate_refs=[],
        canonical_ids=["frpl_pct"],
        source="test",
        provenance="test fixture",
        scenario_ids=[],
        review_status="approved",
    )


def _ba_only_salary_alias_with_frpl_default() -> CatalogAliasRecord:
    """BA-only starting-salary alias that carries an FRPL contextual default for BA.

    The alias resolves to only metric_id=89 (BA). When degree_lane="ma" is requested,
    lane-filtering produces an empty candidate set → metric=None. The FRPL contextual
    default (metric_id=89) would then be the only resolution path — which the guard
    must block when degree_lane is explicitly set.

    This is the exact production shape the guard protects against:
    - Without guard: FRPL default fires → BA metric fetched silently for MA request.
    - With guard: FRPL default skipped → ExecutionRefusal returned.
    """
    return CatalogAliasRecord(
        alias="starting salary",
        normalized_alias=normalize_district_name_for_resolution("starting salary"),
        entity_type="metric_bundle",
        resolution_status="ambiguous",
        candidate_refs=[{"metric_id": 89}],
        canonical_ids=["89"],
        source="test",
        provenance="test fixture",
        scenario_ids=[],
        review_status="approved",
        metadata={
            "contextual_defaults": {
                "frpl_profile_sort_display": {
                    "metric_id": 89,
                    "note_keys": ["bachelor_starting_salary_default_lane"],
                }
            }
        },
    )


@pytest.mark.asyncio
async def test_frpl_default_skipped_when_degree_lane_explicit() -> None:
    """When MetricSpec.degree_lane is set explicitly, the FRPL contextual default
    (a BA-centric proxy) must NOT be applied — even when the metric is ambiguous
    and the sort field resolves to frpl_pct.

    This test routes through _execute_profile_ordered_metric_ranking (the guarded
    branch) by using profile_fields=[ProfileFieldSpec(name="frpl_pct")] so that
    _profile_selection_step returns a non-None SortStepSpec. The primary metric is
    "starting salary" (ambiguous: BA=89 / MA=96) with degree_lane="ma".

    Regression guard for operations.py `if sort_step.field == "frpl_pct" and
    primary_metric.degree_lane is None` (appears twice). Without `and degree_lane
    is None`, the executor would silently apply the FRPL BA proxy for an MA-lane
    request.

    Verifiable by reverting the guard: the FRPL default WOULD fire (metric_id=89
    fetched), producing ExecutionSuccess with BA rows — making this test fail.

    With the guard:
    - degree_lane=None + frpl_pct sort + ambiguous metric → FRPL default applied.
    - degree_lane="ma" + frpl_pct sort + ambiguous metric → FRPL default SKIPPED →
      clarification/refusal returned; BA metric_id=89 is never fetched.
    """
    frpl_nces_field = NCESFieldCandidate(
        field_key="frpl_pct",
        label="FRPL %",
        data_type="numeric",
        description="Free and reduced-price lunch share.",
    )
    # Provide the BA renderer note so the FRPL default WOULD succeed without the guard.
    # If the guard is removed, `resolve_contextual_metric_default` returns this note,
    # the BA metric (89) is fetched, and the test fails.
    ba_default_note = RendererNote(
        note_key="bachelor_starting_salary_default_lane",
        note_text=(
            "Used bachelor's-degree starting salary for first-year teachers "
            "because no degree lane was specified."
        ),
        source="test",
        review_status="approved",
    )
    repo = FakeDegreeRepository(
        aliases=[
            # BA-only alias with FRPL contextual default: degree_lane="ma" produces
            # zero lane-matching candidates → metric=None → second guard fires.
            _ba_only_salary_alias_with_frpl_default(),
            _frpl_profile_alias(),
        ],
        nces_fields=[frpl_nces_field],
        profile_rank_rows=[
            {
                "district_id": 1,
                "district_name": "Wake County",
                "state": "NC",
                "field_key": "frpl_pct",
                "label": "FRPL %",
                "value": 60.0,
                "display_value": "60%",
                "academic_year": "2024 - 2025",
                "vintage": "NCES release year: 2023",
            },
            {
                "district_id": 2,
                "district_name": "Aldine ISD",
                "state": "TX",
                "field_key": "frpl_pct",
                "label": "FRPL %",
                "value": 75.0,
                "display_value": "75%",
                "academic_year": "2024 - 2025",
                "vintage": "NCES release year: 2023",
            },
        ],
        renderer_notes=[ba_default_note],  # present so default WOULD fire without guard
    )
    executor = DeterministicQueryExecutor(repo)

    # profile_fields=[ProfileFieldSpec(name="frpl_pct")] triggers _profile_selection_step
    # → routes to _execute_profile_ordered_metric_ranking where the FRPL guard lives.
    # "starting salary" alias is BA-only; degree_lane="ma" produces metric=None.
    # sort_step.field resolves to "frpl_pct" → guard condition fires.
    # With guard: degree_lane is not None → skip FRPL default → ExecutionRefusal.
    # Without guard (if `and primary_metric.degree_lane is None` removed):
    #   FRPL default resolves to BA metric_id=89 → ExecutionSuccess with BA rows.
    plan = QueryPlan(
        operation="rank",
        question="Rank districts by FRPL share and show MA starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary", degree_lane="ma")],
        profile_fields=[ProfileFieldSpec(name="frpl_pct")],
    )

    outcome = await executor.execute(plan)

    # With the guard in place: the FRPL BA proxy is skipped → refusal returned.
    # Without the guard: BA metric (89) would be fetched silently → ExecutionSuccess.
    assert isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionRefusal when degree_lane='ma' blocks the BA-centric "
        f"FRPL default, got {type(outcome).__name__}. "
        "If ExecutionSuccess: the guard at operations.py:551 "
        "(`if sort_step.field == 'frpl_pct' and primary_metric.degree_lane is None`) "
        "is not executing. Check that profile_fields routes into "
        "_execute_profile_ordered_metric_ranking and that sort_step.field resolves "
        "to 'frpl_pct'."
    )
    assert 89 not in repo.fetched_metric_ids, (
        "BA metric (89) must not be fetched when degree_lane='ma' is explicit. "
        "The FRPL contextual default (BA proxy) must have been skipped by the guard."
    )


# ---------------------------------------------------------------------------
# Fix 6 (PR 2C review) — degree_lane_applied methodology code emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rank_with_degree_lane_emits_methodology_code() -> None:
    """_execute_ranking must emit 'degree_lane_applied' in methodology_codes
    when MetricSpec.degree_lane is not None.

    Regression guard for operations.py _execute_ranking emit block (PR 2C round 2).
    """
    repo = FakeDegreeRepository(aliases=[_ambiguous_salary_alias()])
    executor = DeterministicQueryExecutor(repo)

    plan = QueryPlan(
        operation="rank",
        question="Rank all districts by MA starting salary.",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary", degree_lane="ma")],
        sort=SortSpec(field="starting salary", direction="desc"),
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    codes = [ref.code for ref in result.methodology_codes]
    assert "degree_lane_applied" in codes, (
        f"Rank with degree_lane='ma' must emit 'degree_lane_applied'. Got: {codes}"
    )
    # Confirm the correct lane is recorded in metadata
    for ref in result.methodology_codes:
        if ref.code == "degree_lane_applied":
            assert ref.metadata.get("degree_lane") == "ma"


@pytest.mark.asyncio
async def test_lookup_with_degree_lane_emits_methodology_code() -> None:
    """_execute_lookup must emit 'degree_lane_applied' in methodology_codes
    when any MetricSpec in the plan has a non-None degree_lane.

    Regression guard for operations.py _execute_lookup emit block (PR 2C round 2).
    """
    repo = FakeDegreeRepository(aliases=[_ambiguous_salary_alias()])
    executor = DeterministicQueryExecutor(repo)

    plan = QueryPlan(
        operation="lookup",
        question="Show MA starting salary for Wake County and Aldine.",
        selection=SelectionSpec(scope="named_districts", districts=["Wake County", "Aldine ISD"]),
        metrics=[MetricSpec(name="starting salary", degree_lane="ma")],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    codes = [ref.code for ref in result.methodology_codes]
    assert "degree_lane_applied" in codes, (
        f"Lookup with degree_lane='ma' must emit 'degree_lane_applied'. Got: {codes}"
    )


@pytest.mark.asyncio
async def test_lookup_without_degree_lane_does_not_emit_methodology_code() -> None:
    """When degree_lane is None (unspecified), the 'degree_lane_applied' methodology
    code must NOT appear in the result. Only emit when the user explicitly set a lane.
    """
    # Use BA alias so metric resolves without ambiguity (degree_lane=None)
    ba_alias = CatalogAliasRecord(
        alias="bachelor starting salary",
        normalized_alias=normalize_district_name_for_resolution("bachelor starting salary"),
        entity_type="metric_bundle",
        resolution_status="approved",
        candidate_refs=[{"metric_id": 89}],
        canonical_ids=["89"],
        source="test",
        provenance="test fixture",
        scenario_ids=[],
        review_status="approved",
    )
    repo_ba = FakeDegreeRepository(aliases=[ba_alias])
    executor_ba = DeterministicQueryExecutor(repo_ba)

    plan = QueryPlan(
        operation="lookup",
        question="Show bachelor starting salary for Wake County.",
        selection=SelectionSpec(scope="named_districts", districts=["Wake County"]),
        metrics=[MetricSpec(name="bachelor starting salary", degree_lane=None)],
    )

    outcome = await executor_ba.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    codes = [ref.code for ref in result.methodology_codes]
    assert "degree_lane_applied" not in codes, (
        "Without an explicit degree_lane, 'degree_lane_applied' must NOT be emitted. "
        f"Got methodology codes: {codes}"
    )


# ---------------------------------------------------------------------------
# Round 3 (review-driven) — degree_lane_applied methodology prose is lane-specific
# ---------------------------------------------------------------------------


def test_degree_lane_disclosure_renders_ma_specifically() -> None:
    """The methodology prose for degree_lane_applied must name the specific lane,
    not 'bachelor's or master's' generically.

    Regression guard for rendering/methodology.py _degree_lane_disclosure_line.
    The static-string version said 'bachelor's or master's'; this test verifies
    the callable reads ref.metadata['degree_lane'] and returns MA-specific prose.
    """
    from compass_backend.artifacts import MethodologyRef  # noqa: PLC0415
    from compass_backend.rendering.methodology import methodology_line_for  # noqa: PLC0415

    ref = MethodologyRef(code="degree_lane_applied", metadata={"degree_lane": "ma"})
    line = methodology_line_for(ref)
    assert "master's-degree" in line, (
        f"Expected 'master's-degree' in methodology prose for lane='ma', got: {line!r}"
    )
    assert "bachelor's-degree" not in line, (
        f"Expected no 'bachelor's-degree' in MA prose, got: {line!r}"
    )


def test_degree_lane_disclosure_renders_ba_specifically() -> None:
    """The methodology prose for degree_lane_applied must name bachelor's-degree
    specifically for lane='ba', not both lanes generically.
    """
    from compass_backend.artifacts import MethodologyRef  # noqa: PLC0415
    from compass_backend.rendering.methodology import methodology_line_for  # noqa: PLC0415

    ref = MethodologyRef(code="degree_lane_applied", metadata={"degree_lane": "ba"})
    line = methodology_line_for(ref)
    assert "bachelor's-degree" in line, (
        f"Expected 'bachelor's-degree' in methodology prose for lane='ba', got: {line!r}"
    )
    assert "master's-degree" not in line, (
        f"Expected no 'master's-degree' in BA prose, got: {line!r}"
    )


def test_degree_lane_disclosure_falls_back_gracefully_for_unknown_lane() -> None:
    """When degree_lane metadata is absent or unexpected, the callable must not
    raise — it returns a generic fallback sentence instead.
    """
    from compass_backend.artifacts import MethodologyRef  # noqa: PLC0415
    from compass_backend.rendering.methodology import methodology_line_for  # noqa: PLC0415

    ref_missing = MethodologyRef(code="degree_lane_applied", metadata={})
    line_missing = methodology_line_for(ref_missing)
    assert isinstance(line_missing, str) and len(line_missing) > 0

    ref_unknown = MethodologyRef(
        code="degree_lane_applied", metadata={"degree_lane": "phd"}
    )
    line_unknown = methodology_line_for(ref_unknown)
    assert isinstance(line_unknown, str) and len(line_unknown) > 0


# ---------------------------------------------------------------------------
# PR B Item 1 — degree_lane emission for trend / peer_comparison / count / similarity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trend_with_degree_lane_emits_methodology_code() -> None:
    """_execute_trend must emit 'degree_lane_applied' in methodology_codes
    when MetricSpec.degree_lane is not None.

    Regression guard for operations.py _execute_trend emit block (PR B Item 1).
    """
    repo = FakeDegreeRepository(aliases=[_ambiguous_salary_alias()])
    executor = DeterministicQueryExecutor(repo)

    plan = QueryPlan(
        operation="trend",
        question="Show trend of MA starting salary for Wake County.",
        selection=SelectionSpec(scope="named_districts", districts=["Wake County"]),
        metrics=[MetricSpec(name="starting salary", degree_lane="ma")],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    codes = [ref.code for ref in result.methodology_codes]
    assert "degree_lane_applied" in codes, (
        f"Trend with degree_lane='ma' must emit 'degree_lane_applied'. Got: {codes}"
    )
    for ref in result.methodology_codes:
        if ref.code == "degree_lane_applied":
            assert ref.metadata.get("degree_lane") == "ma"


@pytest.mark.asyncio
async def test_peer_comparison_with_degree_lane_emits_methodology_code() -> None:
    """_execute_peer_comparison must emit 'degree_lane_applied' in methodology_codes
    when MetricSpec.degree_lane is not None.

    Regression guard for operations.py _execute_peer_comparison emit block (PR B Item 1).
    Uses a FakeCatalogResolver to avoid needing full NCES profile data.
    """
    from compass_backend.catalog import CatalogResolver, PeerSetResolution  # noqa: PLC0415
    from compass_backend.catalog.resolution import (  # noqa: PLC0415
        CoveredNCESProfileRow,
        PeerDistrictCandidate,
    )

    anchor_profile = CoveredNCESProfileRow(
        district_id=1, district_name="Wake County", state="NC"
    )
    peer_profile = PeerDistrictCandidate(
        district_id=2,
        district_name="Aldine ISD",
        state="TX",
        rank=1,
        similarity_score=0.9,
    )
    fake_peer_set = PeerSetResolution(
        anchor=anchor_profile,
        peers=[peer_profile],
    )

    repo = FakeDegreeRepository(aliases=[_ambiguous_salary_alias()])

    class FakeCatalogResolver(CatalogResolver):
        async def resolve_peer_set(
            self,
            anchor_name: str,
            *,
            states: set[str] | None = None,
            limit: int = 5,
            academic_year: str,
            feature_set: str = "all",
            exclude_states: list[str] | None = None,
        ) -> PeerSetResolution | None:
            return fake_peer_set

    catalog = FakeCatalogResolver(repo)
    executor = DeterministicQueryExecutor(repo, catalog_resolver=catalog)

    plan = QueryPlan(
        operation="peer_comparison",
        question="Compare Wake County's MA starting salary to its peers.",
        selection=SelectionSpec(scope="named_districts", districts=["Wake County"]),
        metrics=[MetricSpec(name="starting salary", degree_lane="ma")],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    codes = [ref.code for ref in result.methodology_codes]
    assert "degree_lane_applied" in codes, (
        f"Peer comparison with degree_lane='ma' must emit 'degree_lane_applied'. Got: {codes}"
    )
    for ref in result.methodology_codes:
        if ref.code == "degree_lane_applied":
            assert ref.metadata.get("degree_lane") == "ma"


@pytest.mark.asyncio
async def test_count_with_degree_lane_emits_methodology_code() -> None:
    """_execute_count (metric count path) must emit 'degree_lane_applied' in
    methodology_codes when MetricSpec.degree_lane is not None.

    Regression guard for operations.py _execute_count emit block (PR B Item 1).
    Uses the metric-count path (count qualifying districts for a metric).
    """
    repo = FakeDegreeRepository(aliases=[_ambiguous_salary_alias()])
    executor = DeterministicQueryExecutor(repo)

    plan = QueryPlan(
        operation="count",
        question="How many districts have MA starting salary data?",
        selection=SelectionSpec(scope="all_covered_districts"),
        metrics=[MetricSpec(name="starting salary", degree_lane="ma")],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    codes = [ref.code for ref in result.methodology_codes]
    assert "degree_lane_applied" in codes, (
        f"Count with degree_lane='ma' must emit 'degree_lane_applied'. Got: {codes}"
    )
    for ref in result.methodology_codes:
        if ref.code == "degree_lane_applied":
            assert ref.metadata.get("degree_lane") == "ma"


@pytest.mark.asyncio
async def test_similarity_with_degree_lane_emits_methodology_code() -> None:
    """_execute_similarity must emit 'degree_lane_applied' in methodology_codes
    when plan.metrics is non-empty and plan.metrics[0].degree_lane is not None.

    Regression guard for operations.py _execute_similarity emit block (PR B Item 1).
    Uses a FakeCatalogResolver to avoid needing full NCES profile data.
    """
    from compass_backend.contracts import SimilarityQuerySpec  # noqa: PLC0415
    from compass_backend.catalog import CatalogResolver, PeerSetResolution  # noqa: PLC0415
    from compass_backend.catalog.resolution import (  # noqa: PLC0415
        CoveredNCESProfileRow,
        PeerDistrictCandidate,
    )

    anchor_profile = CoveredNCESProfileRow(
        district_id=1, district_name="Wake County", state="NC"
    )
    peer_profile = PeerDistrictCandidate(
        district_id=2,
        district_name="Aldine ISD",
        state="TX",
        rank=1,
        similarity_score=0.9,
    )
    fake_peer_set = PeerSetResolution(
        anchor=anchor_profile,
        peers=[peer_profile],
    )

    repo = FakeDegreeRepository(aliases=[_ambiguous_salary_alias()])

    class FakeCatalogResolver(CatalogResolver):
        async def resolve_peer_set(
            self,
            anchor_name: str,
            *,
            states: set[str] | None = None,
            limit: int = 5,
            academic_year: str,
            feature_set: str = "all",
            exclude_states: list[str] | None = None,
        ) -> PeerSetResolution | None:
            return fake_peer_set

    catalog = FakeCatalogResolver(repo)
    executor = DeterministicQueryExecutor(repo, catalog_resolver=catalog)

    # Similarity plan with an explicit degree_lane on the metric.
    # Similarity does not use metrics for peer-set resolution, but the plan
    # may carry metrics for transparency. The emission guard fires when
    # plan.metrics is non-empty and degree_lane is set.
    plan = QueryPlan(
        operation="similarity",
        question="Find peers to Wake County similar in MA starting salary context.",
        selection=SelectionSpec(scope="named_districts", districts=["Wake County"]),
        similarity=SimilarityQuerySpec(
            anchor_name="Wake County",
            feature_set="all",
            limit=5,
        ),
        metrics=[MetricSpec(name="starting salary", degree_lane="ba")],
    )

    outcome = await executor.execute(plan)

    assert not isinstance(outcome, ExecutionRefusal), (
        f"Expected ExecutionSuccess, got ExecutionRefusal: {outcome.message}"
    )
    assert not isinstance(outcome, ExecutionClarification), (
        f"Expected ExecutionSuccess, got ExecutionClarification: {outcome.message}"
    )
    result = outcome.result
    assert result is not None
    codes = [ref.code for ref in result.methodology_codes]
    assert "degree_lane_applied" in codes, (
        f"Similarity with plan.metrics[0].degree_lane='ba' must emit "
        f"'degree_lane_applied'. Got: {codes}"
    )
    for ref in result.methodology_codes:
        if ref.code == "degree_lane_applied":
            assert ref.metadata.get("degree_lane") == "ba"
