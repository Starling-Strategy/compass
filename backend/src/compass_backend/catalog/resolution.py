"""Universal catalog resolution contracts for the fresh Compass pipeline."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

if TYPE_CHECKING:
    from compass_backend.catalog.recall import (
        CatalogRecallService,
        RecallEntityType,
        RecallReport,
    )
    from compass_backend.contracts.planning import DegreeLane

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import ModelRetry, UnexpectedModelBehavior, UserError

from compass_backend.observability import compass_span, set_span_attributes
from compass_backend.catalog.adjudication import (
    CatalogAdjudicationCandidate,
    CatalogAdjudicationDecision,
    CatalogAdjudicationDeps,
    create_catalog_adjudicator_agent,
    validate_catalog_adjudication_decision,
)
from compass_backend.catalog.candidates import (
    CandidateSearchBatch,
    reciprocal_rank_fuse_metric_candidates,
)
from compass_backend.catalog.district_normalization import (
    # Re-exported via compass_backend.catalog.__init__ (kept in its __all__).
    normalize_district_name_for_resolution as normalize_district_name_for_resolution,
)
from compass_backend.catalog.reporting import CatalogResolutionEntity
from compass_backend.reference import (
    normalize_whitespace_casefold,
    split_state_suffix,
)

_METRIC_QUERY_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_DEFAULT_CATALOG_ADJUDICATOR = object()
# Topic-frame fetch overshoots the caller's limit so the frame has enough rows
# to group/rank before trimming. Multiplier scales with demand; floor guards
# tiny limits. Higher = better topic recall, more DB work per query.
_TOPIC_FRAME_FETCH_MULTIPLIER = 2
_TOPIC_FRAME_FETCH_MIN = 10
# Candidate-fusion search casts a wider net than the caller's limit so fusion
# has enough per-query hits to merge/dedupe. Higher = better recall, more cost.
_CANDIDATE_FUSION_SEARCH_MULTIPLIER = 3
_CANDIDATE_FUSION_SEARCH_MIN = 15

# Degree-lane token sets used to live here as `_LANE_TOKENS_BA/_MA`. They are
# now the single source of truth in `catalog/degree_lane.py`, which both this
# module and `execution/_salary_disclosure.py` consume — see audit finding #17.
# STOPGAP: this stays token-based until `degree_lane` becomes a first-class
# column on `compass.navigator_metrics` (future PR 2D+).
# TODO(PR 2D+): promote `degree_lane` to a first-class DB column on
#               navigator_metrics and delete `catalog/degree_lane.py`; this
#               token classifier is the interim shim, not the destination.
# Observation candidate-search token sets (CLAUDE.md "No prose-dispatch
# below the planner"). These tokens classify planner-emitted MetricSpec.name
# strings to decide whether to ADD a canned observation query to the catalog
# candidate pool in `_observation_candidate_search_query`. They never
# subtract, substitute, or route — the original MetricSpec.name is always
# still searched; the canned query is appended. See the function docstring
# for the doctrine link.
_OBSERVATION_CANDIDATE_CANNED_QUERY = (
    "Minimum number of formal observations per evaluation cycle "
    "for non-tenured teachers"
)
_OBSERVATION_CANDIDATE_BLOCKED_TOKENS = {
    "feedback",
    "informal",
    "rating",
    "summative",
    "walkthrough",
    "walkthroughs",
}
_OBSERVATION_ACTION_TOKENS = {
    "observe",
    "observed",
    "observes",
    "observing",
    "see",
    "seeing",
    "sees",
    "watch",
    "watched",
    "watches",
    "watching",
}
_OBSERVATION_CANDIDATE_CONTEXT_TOKENS = {
    "classroom",
    "formal",
    "require",
    "required",
    "teacher",
    "teachers",
    "teach",
    "teaches",
    "teaching",
    "tenured",
}
_OBSERVATION_CANDIDATE_QUANTITY_TOKENS = {
    "count",
    "counts",
    "frequency",
    "many",
    "number",
}
_OBSERVATION_STATUS_TOKENS = {
    "nonprobationary",
    "nontenured",
    "probationary",
    "tenured",
}
_METRIC_ALIAS_FILLER_TOKENS = {
    "a",
    "an",
    "are",
    "be",
    "being",
    "is",
    "the",
}
CatalogEntityType = Literal[
    "district",
    "metric",
    "metric_bundle",
    "topic",
    "glossary_term",
    "nces_field",
    "unsupported_concept",
]
# Resolver's per-resolution outcome for one phrase. Distinct from
# ``catalog.reporting.CatalogReportStatus`` (the report-layer rollup status) —
# see #1588: the two intentionally model different stages and member sets.
CatalogResolutionStatus = Literal[
    "approved",
    "ambiguous",
    "conditional",
    "out_of_universe",
    "rejected",
]

class MetricCandidate(BaseModel):
    """Metric catalog candidate returned before execution can use a metric ID."""

    model_config = ConfigDict(extra="forbid")

    metric_id: int
    name: str = Field(min_length=1)
    answer_type: str | None = None
    topic: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MetricResolution(BaseModel):
    """Resolution result for one planner-provided metric phrase."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    resolved: MetricCandidate | None = None
    candidates: list[MetricCandidate] = Field(default_factory=list)
    ambiguous: bool = False
    ambiguity_key: str | None = None
    clarification_hint: str | None = Field(
        default=None,
        description=(
            "Adjudicator-supplied clarification phrasing, populated when the "
            "catalog adjudicator emits action='clarify'. Passed to the clarify "
            "stylist as input context."
        ),
    )

    @property
    def ok(self) -> bool:
        """Return whether the metric resolved to execution authority."""

        return self.resolved is not None and not self.ambiguous


class MetricBundleResolution(BaseModel):
    """Resolution result for one governed metric phrase or bundle."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    bundle_key: str | None = None
    resolved: list[MetricCandidate] = Field(default_factory=list)
    candidates: list[MetricCandidate] = Field(default_factory=list)
    alternate_candidates: list[MetricCandidate] = Field(
        default_factory=list,
        description=(
            "Metric candidates the adjudicator marked as adjacent variants of "
            "the resolved primary metric. Populated only when the adjudicator "
            "emits action='select_with_alternates'. These are not executed; "
            "the answer stylist may name them as alternatives in the response."
        ),
    )
    ambiguous: bool = False
    topic_frame: TopicFrame | None = None
    clarification_hint: str | None = Field(
        default=None,
        description=(
            "Adjudicator-supplied clarification phrasing, populated when the "
            "catalog adjudicator emits action='clarify'. Passed to the clarify "
            "stylist as input context."
        ),
    )

    @property
    def ok(self) -> bool:
        """Return whether every bundle metric resolved to execution authority."""

        return bool(self.resolved) and not self.ambiguous


class RendererNote(BaseModel):
    """Approved renderer copy looked up by governed note key."""

    model_config = ConfigDict(extra="forbid")

    note_key: str = Field(min_length=1)
    note_text: str = Field(min_length=1)
    source: str = Field(min_length=1)
    provenance: str = ""
    scenario_ids: list[str] = Field(default_factory=list)
    review_status: str = Field(min_length=1)
    active: bool = True


class DistrictCandidate(BaseModel):
    """Covered district candidate returned before execution can use IDs."""

    model_config = ConfigDict(extra="forbid")

    district_id: int
    district_name: str = Field(min_length=1)
    state: str | None = None
    city: str | None = None
    county_name: str | None = None
    enrollment: int | None = None
    locale_type: str | None = None
    match_reason: str | None = None


class NcesDistrictMatch(BaseModel):
    """One grounded NCES district matching an ambiguous name, with coverage status.

    Unlike :class:`DistrictCandidate` — a *covered* district with a usable
    ``district_id`` — this represents ANY district in the full NCES universe.
    It exists to disambiguate a name that has no covered match (the dominant
    case: ~280 multi-state names have zero covered districts). It carries the
    ``leaid`` (not a covered ``district_id``), the state, and a ``covered``
    flag so a clarification can name the real candidates by state and say
    plainly whether any are in the Pathfinder. An uncovered match is NEVER
    promoted to a clickable answer option (no ``district_id`` to resolve to) —
    it appears only as grounded prose.
    """

    model_config = ConfigDict(extra="forbid")

    leaid: str = Field(min_length=1)
    district_name: str = Field(min_length=1)
    state: str | None = None
    enrollment: int | None = None
    covered: bool = False


class DistrictResolution(BaseModel):
    """Resolution result for planner-provided district names."""

    model_config = ConfigDict(extra="forbid")

    resolved: list[DistrictCandidate] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    ambiguous: dict[str, list[DistrictCandidate]] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Return whether every requested name resolved to one district."""

        return not self.unresolved and not self.ambiguous


class SourceDocumentCandidate(BaseModel):
    """Source document candidate returned before citations/execution use source IDs."""

    model_config = ConfigDict(extra="forbid")

    source_id: int
    title: str = Field(min_length=1)
    district_id: int | None = None
    document_type: str | None = None
    academic_year: str | None = None
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TopicCandidate(BaseModel):
    """Topic or subtopic candidate from the Compass topic catalog."""

    model_config = ConfigDict(extra="forbid")

    topic_id: int
    topic_name: str = Field(min_length=1)
    subtopic_id: int | None = None
    subtopic_name: str | None = None
    question_count: int | None = None


class TopicContentLink(BaseModel):
    """Approved content linked to a governed topic frame."""

    model_config = ConfigDict(extra="forbid")

    topic_id: int
    topic_name: str = Field(min_length=1)
    subtopic_id: int | None = None
    subtopic_name: str | None = None
    content_type: str = Field(min_length=1)
    content_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    source: str = Field(min_length=1)
    provenance: str = ""


class ResolvedTopicAuthority(BaseModel):
    """Internal topic authority used only for diagnostics and narrowing."""

    model_config = ConfigDict(extra="forbid")

    topic_ids: list[int] = Field(default_factory=list)
    subtopic_ids: list[int] = Field(default_factory=list)
    metric_ids: list[int] = Field(default_factory=list)
    related_content: list[TopicContentLink] = Field(default_factory=list)


class TopicFrame(BaseModel):
    """Resolver-owned topic context that may narrow candidates, not execute."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    candidates: list[TopicCandidate] = Field(default_factory=list)
    metric_candidates: list[MetricCandidate] = Field(default_factory=list)
    authority: ResolvedTopicAuthority | None = None
    provenance: str = "CatalogResolver.topic_narrowing"


class GlossaryTermCandidate(BaseModel):
    """Glossary term candidate returned before explanations use a term key."""

    model_config = ConfigDict(extra="forbid")

    term_id: str = Field(min_length=1)
    term: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    context: str | None = None


class NCESFieldCandidate(BaseModel):
    """Approved NCES profile field key candidate."""

    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    data_type: str = Field(min_length=1)
    description: str | None = None


class ProfileFieldResolution(BaseModel):
    """Resolution result for one NCES/profile field phrase."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    resolved: NCESFieldCandidate | None = None
    candidates: list[NCESFieldCandidate] = Field(default_factory=list)
    resolution_method: Literal["alias", "exact", "unresolved"] = "unresolved"

    @property
    def ok(self) -> bool:
        """Return whether the profile field resolved to execution authority."""

        return self.resolved is not None


class CatalogConceptAuthority(BaseModel):
    """Cross-catalog authority for one planner phrase.

    This is a diagnostic/review shape, not execution authority by itself. It
    lets the planner boundary ask whether a phrase the model put in one typed
    slot is actually approved in another catalog drawer.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    metric: MetricResolution | None = None
    metric_bundle: MetricBundleResolution | None = None
    district: DistrictResolution | None = None
    profile_field: ProfileFieldResolution | None = None

    @property
    def approved_entity_types(self) -> list[str]:
        """Return the approved concept drawers in stable priority order."""

        approved: list[str] = []
        if self.metric is not None and self.metric.ok:
            approved.append("metric")
        if (
            self.metric_bundle is not None
            and self.metric_bundle.ok
            and len(self.metric_bundle.resolved) > 1
        ):
            approved.append("metric_bundle")
        if self.district is not None and self.district.ok and self.district.resolved:
            approved.append("district")
        if self.profile_field is not None and self.profile_field.ok:
            approved.append("profile_field")
        return approved


class ProfileFieldRankRow(BaseModel):
    """One ranked profile-field district row."""

    model_config = ConfigDict(extra="forbid")

    district_id: int
    district_name: str = Field(min_length=1)
    state: str | None = None
    field_key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    value: float
    display_value: str = Field(min_length=1)
    academic_year: str = Field(min_length=1)
    vintage: str | None = None


class ProfileFieldValueRow(BaseModel):
    """One resolved NCES/profile field value for a named district."""

    model_config = ConfigDict(extra="forbid")

    requested_name: str = Field(min_length=1)
    district_id: int | None = None
    district_name: str = Field(min_length=1)
    state: str | None = None
    field_key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    value: str | int | float | None = None
    display_value: str = Field(min_length=1)
    academic_year: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_label: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    covered_by_compass: bool = False


class CoveredNCESProfileRow(BaseModel):
    """Covered district NCES fields used for deterministic peer scoring."""

    model_config = ConfigDict(extra="forbid")

    district_id: int
    district_name: str = Field(min_length=1)
    state: str | None = None
    city: str | None = None
    enrollment: int | None = None
    locale_text: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    total_rev_pp: float | None = None
    total_exp_pp: float | None = None
    pupil_teacher_ratio: float | None = None
    frpl_pct: float | None = None
    directory_year: int | None = None
    finance_year: int | None = None


class PeerFactorScore(BaseModel):
    """One factor in an NCES-style peer score."""

    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=1.0)
    explanation: str = Field(min_length=1)


class PeerDistrictCandidate(CoveredNCESProfileRow):
    """One deterministic covered peer candidate for an anchor district."""

    rank: int = Field(ge=1)
    similarity_score: float = Field(ge=0.0, le=1.0)
    factors: dict[str, PeerFactorScore] = Field(default_factory=dict)


class PeerSetResolution(BaseModel):
    """Resolved anchor plus deterministic peer district set."""

    model_config = ConfigDict(extra="forbid")

    anchor: CoveredNCESProfileRow
    peers: list[PeerDistrictCandidate] = Field(default_factory=list)
    selection_method: str = "nces_similarity"
    policy_version: str = Field(min_length=1, default="nces-similarity-v1-2026-05")


class CatalogAliasRecord(BaseModel):
    """Reviewed catalog alias row from the governed recognition table."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1)
    normalized_alias: str = Field(min_length=1)
    entity_type: CatalogEntityType
    resolution_status: CatalogResolutionStatus
    canonical_id: str | None = None
    canonical_ids: list[str] = Field(default_factory=list)
    candidate_refs: list[dict[str, Any]] = Field(default_factory=list)
    source: str = Field(min_length=1)
    provenance: str = ""
    scenario_ids: list[str] = Field(default_factory=list)
    review_status: str = Field(min_length=1)
    active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def approved(self) -> bool:
        """Return whether this alias can authorize deterministic execution."""

        return (
            self.active
            and self.review_status == "approved"
            and self.resolution_status == "approved"
        )


class ContextualMetricDefault(BaseModel):
    """Approved metric default plus renderer notes for one bounded context."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    context_key: str = Field(min_length=1)
    metric: MetricCandidate
    renderer_notes: list[RendererNote] = Field(min_length=1)
    alias: CatalogAliasRecord


class CatalogRepository(Protocol):
    """Read-only repository operations needed by catalog resolution."""

    async def search_metrics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[MetricCandidate]:
        """Return candidate metrics for a user-facing metric phrase."""

    async def fetch_metrics_by_ids(
        self,
        metric_ids: list[int],
    ) -> list[MetricCandidate]:
        """Return metric candidates by approved catalog IDs."""

    async def search_catalog_aliases(
        self,
        alias: str,
        *,
        entity_types: set[CatalogEntityType],
    ) -> list[CatalogAliasRecord]:
        """Return reviewed aliases for a normalized user-facing phrase."""

    async def fetch_recall_aliases(
        self,
        entity_types: set[CatalogEntityType],
    ) -> list[CatalogAliasRecord]:
        """Return active approved aliases available for advisory recall fanout."""

    async def fetch_renderer_notes(
        self,
        note_keys: list[str],
    ) -> list[RendererNote]:
        """Return approved renderer notes by governed note key."""

    async def resolve_districts(
        self,
        names: list[str],
        *,
        states: set[str] | None = None,
    ) -> DistrictResolution:
        """Resolve covered district names exactly or by normalized name."""

    async def list_covered_districts(
        self,
        *,
        states: set[str] | None = None,
    ) -> list[DistrictCandidate]:
        """Return covered district candidates for deterministic result scope."""

    async def search_district_candidates(
        self,
        query: str,
        *,
        states: set[str] | None = None,
        limit: int = 8,
    ) -> list[DistrictCandidate]:
        """Return broad covered-district candidates for candidate adjudication."""

    async def select_largest_districts(
        self,
        *,
        states: set[str] | None = None,
        limit: int = 5,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        """Return covered districts ordered by approved enrollment authority."""

    async def select_districts_by_enrollment_range(
        self,
        *,
        states: set[str] | None = None,
        min_enrollment: int | None = None,
        max_enrollment: int | None = None,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        """Return covered districts inside an approved enrollment range."""

    async def rank_districts_by_profile_field(
        self,
        field_key: str,
        *,
        limit: int | None = None,
        direction: str = "desc",
        states: set[str] | None = None,
        academic_year: str,
    ) -> list[ProfileFieldRankRow]:
        """Return covered districts ranked by an approved profile field.

        Raises ``UnsupportedRankField`` (from
        ``compass_backend.catalog.profile_rank_fields``) when ``field_key`` is
        not in the governed allowlist. Callers must distinguish ``raises`` from
        ``returns []`` — the empty list means ``no covered districts have a
        value``, while the exception means ``unsupported field shape``.
        """

    async def lookup_nces_profile_values(
        self,
        district_names: list[str],
        *,
        field_key: str,
        states: set[str] | None = None,
        academic_year: str,
    ) -> list[ProfileFieldValueRow]:
        """Return NCES/profile values for named covered or NCES-only districts."""

    async def list_covered_nces_profiles(
        self,
        *,
        academic_year: str,
    ) -> list[CoveredNCESProfileRow]:
        """Return covered district NCES profiles for peer scoring."""

    async def search_source_documents(
        self,
        query: str,
        *,
        district_ids: set[int] | None = None,
        limit: int = 5,
    ) -> list[SourceDocumentCandidate]:
        """Return candidate source documents for a user-facing phrase."""

    async def search_topics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[TopicCandidate]:
        """Return candidate topics or subtopics for a user-facing phrase."""

    async def fetch_topic_metric_candidates(
        self,
        topics: list[TopicCandidate],
        *,
        limit: int = 10,
    ) -> list[MetricCandidate]:
        """Return metric candidates linked to governed topic candidates."""

    async def fetch_topic_content_links(
        self,
        topics: list[TopicCandidate],
        *,
        limit: int = 10,
    ) -> list[TopicContentLink]:
        """Return approved related content linked to governed topic candidates."""

    async def search_glossary_terms(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[GlossaryTermCandidate]:
        """Return candidate glossary terms for a user-facing phrase."""

    async def search_nces_fields(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[NCESFieldCandidate]:
        """Return approved NCES profile field keys for a user-facing phrase."""


class CatalogAdjudicator(Protocol):
    """Small protocol for the bounded catalog adjudication agent."""

    async def run(self, user_prompt: str, **kwargs: Any) -> Any:
        """Run candidate-only adjudication for one catalog phrase."""


class CatalogResolver:
    """Single boundary for turning catalog text into execution-safe IDs."""

    def __init__(
        self,
        repository: CatalogRepository,
        *,
        adjudicator: CatalogAdjudicator | None | object = _DEFAULT_CATALOG_ADJUDICATOR,
        candidate_fusion_enabled: bool = False,
        topic_narrowing_enabled: bool = False,
        recall_service: "CatalogRecallService | None" = None,
    ) -> None:
        self._repository = repository
        self._use_default_adjudicator = adjudicator is _DEFAULT_CATALOG_ADJUDICATOR
        self._adjudicator = (
            None
            if self._use_default_adjudicator
            else cast(CatalogAdjudicator | None, adjudicator)
        )
        self._candidate_fusion_enabled = candidate_fusion_enabled
        self._topic_narrowing_enabled = topic_narrowing_enabled
        self._recall_service = recall_service
        self._recall_reports: list[RecallReport] = []

    async def resolve_unsupported_concepts(
        self,
        phrases: list[str],
    ) -> list[CatalogResolutionEntity]:
        """Return catalog-owned unsupported concepts for exact reviewed phrases."""

        entities: list[CatalogResolutionEntity] = []
        seen: set[str] = set()
        for phrase in phrases:
            key = _normalize_metric_name(phrase)
            if key in seen:
                continue
            await self._run_recall(
                phrase,
                entity_types=("unsupported_concept",),
                limit=5,
            )
            aliases = await self._search_aliases(
                phrase,
                entity_types={"unsupported_concept"},
            )
            blockers = [
                alias
                for alias in aliases
                if (
                    alias.active
                    and alias.review_status == "approved"
                    and alias.resolution_status == "out_of_universe"
                    and alias.canonical_id
                )
            ]
            if len(blockers) != 1:
                continue
            seen.add(key)
            blocker = blockers[0]
            message = _unsupported_concept_message(blocker)
            entities.append(
                CatalogResolutionEntity(
                    input_phrase=phrase,
                    entity_type="unsupported_concept",
                    status="unsupported",
                    resolution_method="unsupported_catalog",
                    approved_key=blocker.canonical_id,
                    label=blocker.alias,
                    provenance="CatalogResolver.resolve_unsupported_concepts",
                    message=message,
                )
            )
        return entities

    async def resolve_districts(
        self,
        names: list[str],
        *,
        states: set[str] | None = None,
    ) -> DistrictResolution:
        """Resolve covered district names through the catalog boundary."""

        exact = await self._repository.resolve_districts(names, states=states)
        if exact.ok:
            return exact

        covered_by_id: dict[int, DistrictCandidate] | None = None
        resolved = list(exact.resolved)
        unresolved: list[str] = []
        ambiguous = dict(exact.ambiguous)

        async def _ensure_covered_index() -> dict[int, DistrictCandidate]:
            nonlocal covered_by_id
            if covered_by_id is None:
                covered_by_id = {
                    district.district_id: district
                    for district in await self._repository.list_covered_districts(
                        states=states
                    )
                }
            return covered_by_id

        for name in exact.unresolved:
            # #1513: a typed "Name, ST" handle — the format Compass itself
            # renders in chips and clarify bullets — re-resolves EXACTLY under
            # its state before any fuzzy fallback, mirroring the recall-side
            # split. Only a unique exact/normalized match is promoted; an
            # ambiguous or empty retry falls through to the existing alias and
            # candidate-search behavior, so fuzzy phrases are never silently
            # substituted.
            remainder, suffix_states = split_state_suffix(name)
            if suffix_states:
                retry = await self._repository.resolve_districts(
                    [remainder],
                    states=(states or set()) | suffix_states,
                )
                if retry.ok and len(retry.resolved) == 1:
                    resolved.append(retry.resolved[0])
                    continue

            aliases = await self._search_aliases(name, entity_types={"district"})
            approved = [alias for alias in aliases if alias.approved]
            unclear = [
                alias
                for alias in aliases
                if alias.resolution_status in {"ambiguous", "conditional"}
            ]
            if len(approved) == 1 and approved[0].canonical_id:
                index = await _ensure_covered_index()
                district = index.get(_int_or_none(approved[0].canonical_id))
                if district is not None:
                    resolved.append(district)
                    continue
            if unclear:
                ambiguous[name] = _district_candidates_from_aliases(unclear)
                continue

            candidates = await self._search_district_candidates(
                name,
                states=states,
                limit=8,
            )
            if candidates:
                # #1563 / #1566: a fuzzy phrase that maps to exactly ONE
                # covered-universe district must resolve directly, not bucket
                # into a degenerate one-option clarification ("I found more
                # than one covered district matching 'DC Public Schools'. ...
                # - DC, DC"). The candidate search casts a wide net and can
                # return non-covered noise (the Cleveland-County symptom: an
                # Oklahoma record alongside the covered NC district), so we
                # filter to the covered index first. Only exactly-one-covered
                # short-circuits; 0-covered and 2+-covered still clarify, and
                # we promote the GROUNDED ``list_covered_districts`` row — not
                # the raw fuzzy candidate, whose name may have drifted — so no
                # invented label or coverage can leak into the answer.
                index = await _ensure_covered_index()
                covered = [
                    candidate
                    for candidate in candidates
                    if candidate.district_id in index
                ]
                if len(covered) == 1:
                    resolved.append(index[covered[0].district_id])
                    continue
                ambiguous[name] = candidates
                continue

            unresolved.append(name)

        return DistrictResolution(
            resolved=resolved,
            unresolved=unresolved,
            ambiguous=ambiguous,
        )

    async def resolve_district(
        self,
        phrase: str,
        *,
        states: set[str] | None = None,
    ) -> DistrictResolution:
        """Resolve a single district phrase. Mirrors `resolve_primary_metric`."""

        return await self.resolve_districts([phrase], states=states)

    async def list_covered_districts(
        self,
        *,
        states: set[str] | None = None,
    ) -> list[DistrictCandidate]:
        """Return covered districts through the catalog boundary."""

        return await self._repository.list_covered_districts(states=states)

    async def search_topics(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list["TopicCandidate"]:
        """Return candidate governed topics for a user-facing phrase."""

        return await self._repository.search_topics(query, limit=limit)

    async def fetch_topic_metric_candidates(
        self,
        topics: list["TopicCandidate"],
        *,
        limit: int = 10,
    ) -> list[MetricCandidate]:
        """Return the governed metric candidates linked to the given topics."""

        return await self._repository.fetch_topic_metric_candidates(
            topics, limit=limit
        )

    def drain_recall_report(self) -> "RecallReport | None":
        """Return and clear resolver-owned advisory recall evidence."""

        if not self._recall_reports:
            return None
        reports = self._recall_reports
        self._recall_reports = []
        if len(reports) == 1:
            return reports[0]
        from compass_backend.catalog.recall import RecallReport

        batches = [
            batch
            for report in reports
            for batch in report.batches
        ]
        return RecallReport(query="CatalogResolver", batches=batches)

    async def _run_recall(
        self,
        query: str,
        *,
        entity_types: tuple["RecallEntityType", ...],
        limit: int,
        states: set[str] | None = None,
    ) -> "RecallReport | None":
        if self._recall_service is None:
            return None
        report = await self._recall_service.recall(
            query,
            entity_types=entity_types,
            limit=limit,
            states=states,
            expand_prompt=False,
        )
        self._recall_reports.append(report)
        return report

    async def _search_district_candidates(
        self,
        query: str,
        *,
        states: set[str] | None,
        limit: int,
    ) -> list[DistrictCandidate]:
        report = await self._run_recall(
            query,
            entity_types=("district",),
            limit=limit,
            states=states,
        )
        if report is not None:
            return _district_candidates_from_recall_report(report)[:limit]

        searcher = getattr(self._repository, "search_district_candidates", None)
        if searcher is None:
            return []
        candidates = await searcher(query, states=states, limit=limit)
        seen: set[int] = set()
        deduped: list[DistrictCandidate] = []
        for candidate in candidates:
            if candidate.district_id in seen:
                continue
            seen.add(candidate.district_id)
            deduped.append(candidate)
        return deduped

    async def search_nces_districts(self, name: str) -> list[NcesDistrictMatch]:
        """Return the prominent NCES districts of an ambiguous name, by state.

        Searches the FULL NCES universe (not covered-only) so a same-name
        ambiguity with no covered match can still be disambiguated with real
        rows. Delegates to the repository's ``search_nces_districts_by_name``;
        returns ``[]`` when the repository can't search NCES (the seam then
        keeps today's drop-ungrounded behavior). The repository owns the match
        strategy (distinctive token + enrollment floor + top-per-state).
        """

        searcher = getattr(self._repository, "search_nces_districts_by_name", None)
        if searcher is None:
            return []
        return await searcher(name)

    async def select_largest_districts(
        self,
        *,
        states: set[str] | None = None,
        limit: int = 5,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        """Return largest covered districts through the catalog boundary."""

        return await self._repository.select_largest_districts(
            states=states,
            limit=limit,
            academic_year=academic_year,
        )

    async def select_districts_by_enrollment_range(
        self,
        *,
        states: set[str] | None = None,
        min_enrollment: int | None = None,
        max_enrollment: int | None = None,
        academic_year: str,
    ) -> list[DistrictCandidate]:
        """Resolve enrollment filters through the catalog boundary."""

        return await self._repository.select_districts_by_enrollment_range(
            states=states,
            min_enrollment=min_enrollment,
            max_enrollment=max_enrollment,
            academic_year=academic_year,
        )

    async def fetch_metrics_by_ids(
        self,
        metric_ids: list[int],
    ) -> list[MetricCandidate]:
        """Return metric candidates by governed catalog IDs."""

        return await self._repository.fetch_metrics_by_ids(metric_ids)

    async def rank_districts_by_profile_field(
        self,
        field_key: str,
        *,
        limit: int | None = None,
        direction: str = "desc",
        states: set[str] | None = None,
        academic_year: str,
    ) -> list[ProfileFieldRankRow]:
        """Rank covered districts by a governed profile field.

        Propagates ``UnsupportedRankField`` from the underlying repository when
        ``field_key`` is not in the governed allowlist. The executor's
        ``_rank_profile_rows`` catches and converts to ``ExecutionRefusal``.
        """

        rows = await self._repository.rank_districts_by_profile_field(
            field_key,
            limit=limit,
            direction=direction,
            states=states,
            academic_year=academic_year,
        )
        return [ProfileFieldRankRow.model_validate(row) for row in rows]

    async def resolve_profile_field_authority(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> ProfileFieldResolution:
        """Resolve one approved NCES/profile field phrase with provenance."""

        searcher = getattr(self._repository, "search_nces_fields", None)
        if searcher is None:
            return ProfileFieldResolution(query=query)

        alias = await self._profile_field_alias(query)
        if alias is not None and alias.canonical_id:
            candidates = await self._search_profile_field_candidates(
                alias.canonical_id,
                limit=limit,
            )
            for candidate in candidates:
                if candidate.field_key == alias.canonical_id:
                    return ProfileFieldResolution(
                        query=query,
                        resolved=candidate,
                        candidates=candidates,
                        resolution_method="alias",
                    )
            return ProfileFieldResolution(query=query, candidates=candidates)

        candidates = await self._search_profile_field_candidates(query, limit=limit)
        exact = _single_exact_nces_field_match(query, candidates)
        if exact is not None:
            return ProfileFieldResolution(
                query=query,
                resolved=exact,
                candidates=candidates,
                resolution_method="exact",
            )
        return ProfileFieldResolution(query=query, candidates=candidates)

    async def _search_profile_field_candidates(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[NCESFieldCandidate]:
        report = await self._run_recall(
            query,
            entity_types=("profile_field",),
            limit=limit,
        )
        if report is not None:
            return _profile_field_candidates_from_recall_report(report)[:limit]
        return await self._repository.search_nces_fields(query, limit=limit)

    async def lookup_nces_profile_values(
        self,
        district_names: list[str],
        *,
        field_key: str,
        states: set[str] | None = None,
        academic_year: str,
    ) -> list[ProfileFieldValueRow]:
        """Return named-district NCES/profile values through the catalog boundary."""

        rows = await self._repository.lookup_nces_profile_values(
            district_names,
            field_key=field_key,
            states=states,
            academic_year=academic_year,
        )
        return [ProfileFieldValueRow.model_validate(row) for row in rows]

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
        """Resolve one covered anchor and a deterministic NCES-style peer set.

        Args:
            anchor_name: Human-readable anchor district name.
            states: Optional positive state filter for district resolution and
                peer candidates (separate from ``exclude_states``).
            limit: Maximum number of peer districts to return.
            academic_year: Academic year for NCES profile data.
            feature_set: Which NCES factor group to bias toward.  See
                ``catalog.peer_scoring.effective_weights`` for semantics.
                Defaults to 'all' (full governed policy weights — no change
                from the pre-PR 2B behavior).
            exclude_states: State abbreviations to exclude from the candidate
                pool before scoring (pre-scoring exclusion).  ``None`` means
                no exclusion (backward-compat default).
        """

        resolution = await self.resolve_districts([anchor_name], states=states)
        if not resolution.ok or len(resolution.resolved) != 1:
            return None
        anchor_district = resolution.resolved[0]

        rows = await self._repository.list_covered_nces_profiles(
            academic_year=academic_year,
        )
        profiles = [CoveredNCESProfileRow.model_validate(row) for row in rows]
        anchor = next(
            (
                profile
                for profile in profiles
                if profile.district_id == anchor_district.district_id
            ),
            None,
        )
        if anchor is None:
            return None

        from compass_backend.catalog.peer_scoring import (
            active_peer_scoring_policy,
            effective_weights,
        )
        from compass_backend.catalog.peers import build_peer_set

        policy = active_peer_scoring_policy()
        weights_override = (
            effective_weights(policy, feature_set)
            if feature_set != "all"
            else None
        )

        peer_set = build_peer_set(
            anchor,
            profiles,
            limit=limit,
            policy=policy,
            effective_weights_override=weights_override,
            include_states=states,
            exclude_states=exclude_states,
        )
        if not peer_set.peers:
            return None
        return peer_set

    async def resolve_primary_metric(
        self,
        query: str,
        *,
        numeric_only: bool = True,
        limit: int = 5,
        degree_lane: "DegreeLane | None" = None,
    ) -> MetricResolution:
        """Resolve one metric phrase to a metric candidate that execution may use."""

        bundle_resolution = await self.resolve_metric_bundle(
            query,
            numeric_only=numeric_only,
            limit=limit,
            degree_lane=degree_lane,
        )
        if bundle_resolution.ambiguous:
            return MetricResolution(
                query=query,
                candidates=bundle_resolution.candidates,
                ambiguous=True,
                ambiguity_key=bundle_resolution.bundle_key,
                clarification_hint=bundle_resolution.clarification_hint,
            )
        if len(bundle_resolution.resolved) == 1:
            return MetricResolution(
                query=query,
                resolved=bundle_resolution.resolved[0],
                candidates=bundle_resolution.candidates,
            )
        return MetricResolution(
            query=query,
            resolved=None,
            candidates=bundle_resolution.candidates,
            ambiguous=bool(bundle_resolution.resolved),
            ambiguity_key=bundle_resolution.bundle_key,
            clarification_hint=bundle_resolution.clarification_hint,
        )

    async def resolve_metric_bundle(
        self,
        query: str,
        *,
        numeric_only: bool = False,
        limit: int = 5,
        degree_lane: "DegreeLane | None" = None,
    ) -> MetricBundleResolution:
        """Resolve governed launch-scope metric phrases to metric IDs.

        When ``degree_lane`` is set, candidate results are filtered to matching
        lane BEFORE adjudication. This resolves synthetic planner phrases like
        ``"master's degree starting salary"`` that don't exactly match a catalog
        alias — the lane signal disambiguates the ambiguous [BA, MA] candidate set.

        Filter is applied AFTER alias resolution so governed explicit aliases
        (e.g. ``"MA starting salary" → metric_id=96``) are still preferred.
        Lane-neutral candidates are always kept.
        """

        candidates = await self._search_metric_candidates(query, limit=limit)
        if not candidates:
            # Degree-lane recovery (#1, refusal family): the planner folds a degree
            # qualifier into the metric phrase ("starting salary BA degree"), and
            # that qualifier-extended phrase misses BOTH the recall-alias layer
            # (aliased on "starting salary", not the extension) and the lexical
            # search (its stopword/coverage gate drops "ba"/"degree", and the
            # governed wording is "base salary", never "starting") → zero
            # candidates → confidence "none" → finalizer blocker → canned rescue,
            # even though the bare phrase resolves fine. Recover through the TYPED
            # degree-lane authority: strip the lane tokens, re-search the
            # lane-neutral phrase, and carry the lane forward so the existing
            # `_filter_candidates_by_degree_lane` path below collapses the
            # recovered [BA, MA] bundle to the single correct-lane metric. Reads
            # only typed fields (the metric-phrase string + the classifier); no
            # prose dispatch. classify_metric_lane's token authority is symmetric
            # BA/MA, unlike db/catalog.py's stopword set.
            from compass_backend.catalog.degree_lane import (
                classify_metric_lane,
                strip_degree_tokens,
            )

            lane = classify_metric_lane(query)
            if lane is not None:
                stripped = strip_degree_tokens(query)
                if stripped and stripped != query.casefold():
                    candidates = await self._search_metric_candidates(
                        stripped, limit=limit
                    )
                    degree_lane = degree_lane or lane
        exact_metric = _single_exact_metric_match(
            query,
            candidates,
            numeric_only=numeric_only,
        )
        if exact_metric is not None:
            # Exact match: apply lane filter; if conflict → empty resolved list.
            if degree_lane is not None:
                if _candidate_lane(exact_metric) not in (None, degree_lane):
                    return MetricBundleResolution(
                        query=query,
                        resolved=[],
                        candidates=_filter_candidates_by_degree_lane(
                            candidates, degree_lane
                        ),
                    )
            return MetricBundleResolution(
                query=query,
                resolved=[exact_metric],
                candidates=candidates,
            )

        alias = await self._metric_authority_alias(query)
        if alias is None:
            topic_frame = await self._topic_frame_for_metric_query(
                query,
                candidates,
                limit=max(
                    limit * _TOPIC_FRAME_FETCH_MULTIPLIER, _TOPIC_FRAME_FETCH_MIN
                ),
            )
            if topic_frame is not None:
                candidates = _apply_topic_frame_to_candidates(
                    candidates,
                    topic_frame,
                )
            # Apply lane filter before observation-candidate check and adjudication
            if degree_lane is not None:
                candidates = _filter_candidates_by_degree_lane(candidates, degree_lane)
            observation_candidates = _clarifying_observation_metric_candidates(
                query,
                candidates,
                numeric_only=numeric_only,
            )
            if observation_candidates:
                return MetricBundleResolution(
                    query=query,
                    resolved=[],
                    candidates=observation_candidates,
                    ambiguous=True,
                    topic_frame=topic_frame,
                )
            return await self._adjudicated_metric_bundle_resolution(
                query,
                candidates=candidates,
                numeric_only=numeric_only,
                topic_frame=topic_frame,
            )

        if alias.resolution_status == "ambiguous":
            metric_ids = _candidate_metric_ids_for_alias(alias)
            resolved = await self._repository.fetch_metrics_by_ids(metric_ids)
            ordered = _ordered_alias_metrics(
                metric_ids,
                resolved,
                numeric_only=numeric_only,
            )
            if not ordered:
                return MetricBundleResolution(
                    query=query,
                    bundle_key=_alias_bundle_key(alias),
                    resolved=[],
                    candidates=candidates + resolved,
                    ambiguous=False,
                )
            # Apply lane filter: if degree_lane is set, this may disambiguate
            # [BA, MA] into a single resolved metric instead of clarifying.
            if degree_lane is not None:
                lane_filtered = _filter_candidates_by_degree_lane(ordered, degree_lane)
                dropped_siblings = _opposite_lane_candidates(ordered, degree_lane)
                if len(lane_filtered) == 1:
                    # Lane uniquely disambiguates — return as resolved.
                    # Dropped opposite-lane siblings flow through as
                    # alternate_candidates so the renderer can footnote
                    # "MA data also available" when planner defaulted to BA.
                    return MetricBundleResolution(
                        query=query,
                        bundle_key=_alias_bundle_key(alias),
                        resolved=lane_filtered,
                        candidates=lane_filtered,
                        alternate_candidates=dropped_siblings,
                        ambiguous=False,
                    )
                # Zero or multiple lane-matching candidates → preserve ambiguity
                # (zero means conflict, which is handled upstream as refusal).
                return MetricBundleResolution(
                    query=query,
                    bundle_key=_alias_bundle_key(alias),
                    resolved=[],
                    candidates=lane_filtered,
                    ambiguous=bool(lane_filtered),
                )
            return MetricBundleResolution(
                query=query,
                bundle_key=_alias_bundle_key(alias),
                resolved=[],
                candidates=ordered,
                ambiguous=True,
            )

        metric_ids = _metric_ids_for_alias(alias)
        resolved = await self._repository.fetch_metrics_by_ids(metric_ids)
        ordered = _ordered_alias_metrics(
            metric_ids,
            resolved,
            numeric_only=numeric_only,
        )
        if len(ordered) != len(metric_ids):
            return MetricBundleResolution(
                query=query,
                bundle_key=_alias_bundle_key(alias),
                resolved=[],
                candidates=candidates + resolved,
            )

        # Approved single-metric alias: apply lane conflict check.
        if degree_lane is not None:
            lane_filtered = _filter_candidates_by_degree_lane(ordered, degree_lane)
            if not lane_filtered:
                return MetricBundleResolution(
                    query=query,
                    bundle_key=_alias_bundle_key(alias),
                    resolved=[],
                    candidates=candidates + resolved,
                )
        return MetricBundleResolution(
            query=query,
            bundle_key=_alias_bundle_key(alias),
            resolved=ordered,
            candidates=candidates + resolved,
        )

    async def clarifying_observation_metric_bundle(
        self,
        query: str,
        *,
        numeric_only: bool = False,
        limit: int = 5,
    ) -> MetricBundleResolution | None:
        """Surface a typed observation-family ambiguity for a raw question.

        Broad observation phrasings ("how many districts watch teachers
        teach?") name a *family* of governed observation metrics rather than
        one, so they must clarify before execution. Detecting that breadth
        requires reading the question text — which is the catalog's job, not
        the executor's. The executor used to run this prose check itself
        (``execution/scoping.py``), a guardrail #4 violation (#1008); the
        check now lives here, beside the identical
        ``requires_observation_metric_clarification`` gate already used by
        ``_clarifying_observation_metric_candidates`` for candidate broadening.

        Returns the typed ``MetricBundleResolution`` when the question resolves
        to an observation-family ambiguity (the caller clarifies from
        ``.candidates``), else ``None`` — so the caller consumes only typed
        output. The leading gate short-circuits before any candidate search,
        preserving the pre-#1008 cost profile (a bundle resolve runs only for
        genuinely broad observation phrasings).
        """

        if not requires_observation_metric_clarification(query):
            return None
        resolution = await self.resolve_metric_bundle(
            query,
            numeric_only=numeric_only,
            limit=limit,
        )
        if not resolution.ambiguous:
            return None
        return resolution

    async def resolve_contextual_metric_default(
        self,
        query: str,
        *,
        context_key: str,
        numeric_only: bool = False,
    ) -> ContextualMetricDefault | None:
        """Resolve a governed context-specific default from alias metadata.

        Contextual defaults are narrower than normal metric aliases: broad
        aliases can stay ambiguous for standalone execution while explicitly
        authorizing one metric plus disclosure notes in a bounded context.
        """

        for alias_query in _metric_alias_lookup_queries(query):
            aliases = await self._search_aliases(
                alias_query,
                entity_types={"metric", "metric_bundle"},
            )
            defaults = [
                (alias, default_payload)
                for alias in aliases
                if alias.active and alias.review_status == "approved"
                for default_payload in [
                    _contextual_default_payload(alias, context_key)
                ]
                if default_payload is not None
            ]
            if len(defaults) != 1:
                if defaults:
                    return None
                continue

            alias, default_payload = defaults[0]
            metric_id = _int_or_none(default_payload.get("metric_id"))
            note_keys = [
                str(value)
                for value in default_payload.get("note_keys", [])
                if isinstance(value, str) and value
            ]
            if metric_id is None or not note_keys:
                return None
            if not _alias_authorizes_metric_id(alias, metric_id):
                return None

            metrics = await self._repository.fetch_metrics_by_ids([metric_id])
            if len(metrics) != 1:
                return None
            metric = metrics[0]
            if numeric_only and (metric.answer_type or "").casefold() != "numeric":
                return None

            notes = await self._repository.fetch_renderer_notes(note_keys)
            notes_by_key = {note.note_key: note for note in notes if note.active}
            if set(notes_by_key) != set(note_keys):
                return None

            return ContextualMetricDefault(
                query=query,
                context_key=context_key,
                metric=metric,
                renderer_notes=[notes_by_key[key] for key in note_keys],
                alias=alias,
            )
        return None

    async def _search_metric_candidates(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[MetricCandidate]:
        """Return bounded code-generated metric candidates for adjudication."""

        candidates: list[MetricCandidate] = []
        seen: set[int] = set()
        if self._candidate_fusion_enabled:
            batches: list[CandidateSearchBatch[MetricCandidate]] = []
            search_limit = max(
                limit * _CANDIDATE_FUSION_SEARCH_MULTIPLIER,
                _CANDIDATE_FUSION_SEARCH_MIN,
            )
            for search_query in _metric_candidate_search_queries(query):
                results = await self._metric_candidates_for_query(
                    search_query,
                    limit=search_limit,
                )
                batches.append(
                    CandidateSearchBatch(
                        query=search_query,
                        candidates=results,
                    )
                )
            fused = reciprocal_rank_fuse_metric_candidates(
                batches,
                limit=limit,
            )
            return [
                ranked.candidate.model_copy(
                    update={
                        "metadata": {
                            **ranked.candidate.metadata,
                            **ranked.metadata(),
                        }
                    }
                )
                for ranked in fused
            ]
        for search_query in _metric_candidate_search_queries(query):
            for candidate in await self._metric_candidates_for_query(
                search_query,
                limit=limit,
            ):
                if candidate.metric_id in seen:
                    continue
                seen.add(candidate.metric_id)
                candidates.append(candidate)
        return candidates

    async def _metric_candidates_for_query(
        self,
        query: str,
        *,
        limit: int,
    ) -> list[MetricCandidate]:
        report = await self._run_recall(
            query,
            entity_types=("metric", "metric_bundle"),
            limit=limit,
        )
        if report is not None:
            return _metric_candidates_from_recall_report(report)[:limit]
        return await self._repository.search_metrics(query, limit=limit)

    async def _topic_frame_for_metric_query(
        self,
        query: str,
        candidates: list[MetricCandidate],
        *,
        limit: int,
    ) -> TopicFrame | None:
        """Return internal topic context for candidate prioritization only."""

        if not self._topic_narrowing_enabled:
            return None

        topic_searcher = getattr(self._repository, "search_topics", None)
        metric_fetcher = getattr(
            self._repository,
            "fetch_topic_metric_candidates",
            None,
        )
        if topic_searcher is None or metric_fetcher is None:
            return None

        topics = await topic_searcher(query, limit=3)
        if not topics:
            return None

        topic_metrics = await metric_fetcher(topics, limit=limit)
        related_content: list[TopicContentLink] = []
        content_fetcher = getattr(self._repository, "fetch_topic_content_links", None)
        if content_fetcher is not None:
            related_content = await content_fetcher(topics, limit=10)

        topic_ids: list[int] = []
        subtopic_ids: list[int] = []
        for topic in topics:
            if topic.topic_id not in topic_ids:
                topic_ids.append(topic.topic_id)
            if topic.subtopic_id is not None and topic.subtopic_id not in subtopic_ids:
                subtopic_ids.append(topic.subtopic_id)

        metric_ids: list[int] = []
        for metric in topic_metrics:
            if metric.metric_id not in metric_ids:
                metric_ids.append(metric.metric_id)
        for metric in candidates:
            if _candidate_matches_topic(metric, topics) and metric.metric_id not in metric_ids:
                metric_ids.append(metric.metric_id)

        return TopicFrame(
            query=query,
            candidates=topics,
            metric_candidates=topic_metrics,
            authority=ResolvedTopicAuthority(
                topic_ids=topic_ids,
                subtopic_ids=subtopic_ids,
                metric_ids=metric_ids,
                related_content=related_content,
            ),
        )

    async def _adjudicated_metric_bundle_resolution(
        self,
        query: str,
        *,
        candidates: list[MetricCandidate],
        numeric_only: bool,
        topic_frame: TopicFrame | None = None,
    ) -> MetricBundleResolution:
        decision = await self._metric_adjudication_decision(query, candidates)
        if decision is None or decision.action == "no_match":
            return MetricBundleResolution(
                query=query,
                resolved=[],
                candidates=candidates,
                topic_frame=topic_frame,
            )

        if decision.action == "select_with_alternates":
            id_to_candidate = {str(c.metric_id): c for c in candidates}
            primary_id = decision.selected_ids[0] if decision.selected_ids else None
            primary = id_to_candidate.get(primary_id) if primary_id else None
            # Dedupe by metric_id (defensive — Refs #933 intent for new path):
            # the validator forbids overlap between selected_ids and
            # alternate_ids, but two distinct candidate_ids can share a
            # metric_id in the candidate set. Keep first occurrence.
            seen_metric_ids: set[int] = set()
            adjacents: list[MetricCandidate] = []
            for alt_id in decision.alternate_ids:
                candidate = id_to_candidate.get(alt_id)
                if candidate is None or candidate.metric_id in seen_metric_ids:
                    continue
                seen_metric_ids.add(candidate.metric_id)
                adjacents.append(candidate)
            if primary is None or not adjacents:
                # Validator should have caught this; defensive fall-through.
                return MetricBundleResolution(
                    query=query,
                    resolved=[],
                    candidates=candidates,
                    ambiguous=True,
                    topic_frame=topic_frame,
                )
            return MetricBundleResolution(
                query=query,
                resolved=[primary],
                candidates=candidates,
                alternate_candidates=adjacents,
                topic_frame=topic_frame,
            )

        if decision.action == "clarify":
            return MetricBundleResolution(
                query=query,
                resolved=[],
                candidates=candidates,
                ambiguous=True,
                topic_frame=topic_frame,
                clarification_hint=decision.clarification_question,
            )

        selected = _selected_adjudicated_metrics(
            decision,
            candidates,
            numeric_only=numeric_only,
        )
        if not selected:
            return MetricBundleResolution(
                query=query,
                resolved=[],
                candidates=candidates,
                ambiguous=True,
                topic_frame=topic_frame,
            )

        return MetricBundleResolution(
            query=query,
            resolved=selected,
            candidates=candidates,
            topic_frame=topic_frame,
        )

    async def _metric_adjudication_decision(
        self,
        query: str,
        candidates: list[MetricCandidate],
    ) -> CatalogAdjudicationDecision | None:
        with compass_span(
            "compass.catalog.adjudicate",
            query=query,
            candidate_count=len(candidates),
        ) as span:
            if not candidates:
                set_span_attributes(span, outcome="no_candidates")
                return None

            try:
                adjudicator = self._catalog_adjudicator()
                if adjudicator is None:
                    set_span_attributes(span, outcome="no_adjudicator")
                    return None

                adjudication_candidates = tuple(
                    _catalog_adjudication_candidate(candidate) for candidate in candidates
                )
                set_span_attributes(
                    span,
                    candidate_ids=[
                        candidate.candidate_id for candidate in adjudication_candidates
                    ],
                )
                deps = CatalogAdjudicationDeps(
                    query=query,
                    candidates=adjudication_candidates,
                )
                result = await adjudicator.run(query, deps=deps)
                decision = validate_catalog_adjudication_decision(
                    result.output,
                    adjudication_candidates,
                )
                set_span_attributes(
                    span,
                    outcome=decision.action,
                    selected_ids=decision.selected_ids,
                    winner_confidence=decision.confidence,
                )
                return decision
            except (ModelRetry, UnexpectedModelBehavior, UserError) as exc:
                set_span_attributes(
                    span,
                    outcome="error",
                    error_type=type(exc).__name__,
                )
                return CatalogAdjudicationDecision(
                    action="clarify",
                    selected_ids=[],
                    clarification_question=(
                        "Which Compass metric should I use for this request?"
                    ),
                    confidence=0.0,
                    rationale="The adjudicator did not return a valid candidate-only choice.",
                )

    def _catalog_adjudicator(self) -> CatalogAdjudicator | None:
        if self._adjudicator is None and self._use_default_adjudicator:
            self._adjudicator = create_catalog_adjudicator_agent()
        return self._adjudicator

    async def is_multi_metric_bundle(self, query: str) -> bool:
        """Return whether query names a governed bundle with multiple metrics."""

        alias = await self._metric_authority_alias(query)
        return alias is not None and len(_metric_ids_for_alias(alias)) > 1

    async def _search_aliases(
        self,
        alias: str,
        *,
        entity_types: set[CatalogEntityType],
    ) -> list[CatalogAliasRecord]:
        searcher = getattr(self._repository, "search_catalog_aliases", None)
        if searcher is None:
            return []
        return await searcher(alias, entity_types=entity_types)

    async def _metric_authority_alias(
        self,
        query: str,
    ) -> CatalogAliasRecord | None:
        for alias_query in _metric_alias_lookup_queries(query):
            aliases = await self._search_aliases(
                alias_query,
                entity_types={"metric", "metric_bundle"},
            )
            approved = [
                alias
                for alias in aliases
                if (
                    alias.active
                    and alias.review_status == "approved"
                    and (
                        (
                            alias.resolution_status == "approved"
                            and _metric_ids_for_alias(alias)
                        )
                        or (
                            alias.resolution_status == "ambiguous"
                            and _candidate_metric_ids_for_alias(alias)
                        )
                    )
                )
            ]
            if len(approved) == 1:
                return approved[0]
            if approved:
                return None
        return None

    async def _profile_field_alias(
        self,
        query: str,
    ) -> CatalogAliasRecord | None:
        aliases = await self._search_aliases(query, entity_types={"nces_field"})
        approved = [
            alias
            for alias in aliases
            if alias.approved and alias.canonical_id
        ]
        if len(approved) != 1:
            return None
        return approved[0]


def _apply_topic_frame_to_candidates(
    candidates: list[MetricCandidate],
    topic_frame: TopicFrame,
) -> list[MetricCandidate]:
    """Prioritize topic-linked metrics while preserving all existing candidates."""

    authority = topic_frame.authority
    if authority is None or not authority.metric_ids:
        return candidates

    linked_ids = set(authority.metric_ids)

    linked: list[MetricCandidate] = []
    unlinked: list[MetricCandidate] = []
    seen: set[int] = set()
    for candidate in topic_frame.metric_candidates:
        if candidate.metric_id in seen:
            continue
        seen.add(candidate.metric_id)
        linked.append(
            _candidate_with_topic_metadata(
                candidate,
                topic_frame,
                linked=True,
            )
        )
    for candidate in candidates:
        if candidate.metric_id in seen:
            continue
        seen.add(candidate.metric_id)
        updated = _candidate_with_topic_metadata(
            candidate,
            topic_frame,
            linked=candidate.metric_id in linked_ids,
        )
        if candidate.metric_id in linked_ids:
            linked.append(updated)
        else:
            unlinked.append(updated)

    return linked + unlinked


def _candidate_with_topic_metadata(
    candidate: MetricCandidate,
    topic_frame: TopicFrame,
    *,
    linked: bool,
) -> MetricCandidate:
    authority = topic_frame.authority
    if authority is None:
        return candidate
    return candidate.model_copy(
        update={
            "metadata": {
                **candidate.metadata,
                "topic_frame": {
                    "linked": linked,
                    "topic_ids": authority.topic_ids,
                    "subtopic_ids": authority.subtopic_ids,
                    "metric_ids": authority.metric_ids,
                    "provenance": topic_frame.provenance,
                },
            }
        }
    )


def _candidate_matches_topic(
    candidate: MetricCandidate,
    topics: list[TopicCandidate],
) -> bool:
    candidate_topic = _metric_name_key(candidate.topic or "")
    candidate_name = _metric_name_key(candidate.name)
    for topic in topics:
        topic_name = _metric_name_key(topic.topic_name)
        subtopic_name = _metric_name_key(topic.subtopic_name or "")
        if topic_name and candidate_topic == topic_name:
            return True
        if subtopic_name and subtopic_name in candidate_name:
            return True
    return False





def _single_exact_metric_match(
    query: str,
    candidates: list[MetricCandidate],
    *,
    numeric_only: bool,
) -> MetricCandidate | None:
    normalized_query = _normalize_metric_name(query)
    exact_matches = [
        candidate
        for candidate in candidates
        if _normalize_metric_name(candidate.name) == normalized_query
        and (
            not numeric_only
            or (candidate.answer_type or "").casefold() == "numeric"
        )
    ]
    if len(exact_matches) != 1:
        return None
    return exact_matches[0]


def _normalize_metric_name(value: str) -> str:
    return normalize_whitespace_casefold(value)


def _single_exact_nces_field_match(
    query: str,
    candidates: list[NCESFieldCandidate],
) -> NCESFieldCandidate | None:
    normalized_query = _normalize_metric_name(query.replace("_", " "))
    exact_matches = [
        candidate
        for candidate in candidates
        if normalized_query
        in {
            _normalize_metric_name(candidate.field_key.replace("_", " ")),
            _normalize_metric_name(candidate.label),
        }
    ]
    if len(exact_matches) != 1:
        return None
    return exact_matches[0]


def _metric_candidate_search_queries(query: str) -> list[str]:
    queries = [query]
    formal_observation_query = _observation_candidate_search_query(query)
    if formal_observation_query is not None:
        queries.append(formal_observation_query)
    return _dedupe_strings(queries)


def _metric_alias_lookup_queries(query: str) -> list[str]:
    queries = [query]
    tokens = _metric_query_tokens(query)
    content_tokens = [
        token
        for token in tokens
        if token not in _METRIC_ALIAS_FILLER_TOKENS
    ]
    if len(content_tokens) != len(tokens):
        queries.append(" ".join(content_tokens))
    return _dedupe_strings(queries)


def _unsupported_concept_message(alias: CatalogAliasRecord) -> str:
    message = alias.metadata.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return f"Compass does not yet have a governed metric for {alias.alias}."


def _metric_candidates_from_recall_report(
    report: "RecallReport",
) -> list[MetricCandidate]:
    candidates: list[MetricCandidate] = []
    seen: set[int] = set()
    for card in report.candidates:
        if card.entity_type != "metric" or "metric_search" not in card.source_methods:
            continue
        metric_id = _entity_ref_int(card.entity_ref, prefix="metric")
        if metric_id is None or metric_id in seen:
            continue
        seen.add(metric_id)
        candidates.append(
            MetricCandidate(
                metric_id=metric_id,
                name=card.label,
                answer_type=_metadata_str(card.metadata.get("answer_type")),
                topic=_metadata_str(card.metadata.get("topic")),
                metadata={
                    **card.metadata,
                    "recall_source_methods": list(card.source_methods),
                },
            )
        )
    return candidates


def _district_candidates_from_recall_report(
    report: "RecallReport",
) -> list[DistrictCandidate]:
    candidates: list[DistrictCandidate] = []
    seen: set[int] = set()
    for card in report.candidates:
        if card.entity_type != "district" or "district_search" not in card.source_methods:
            continue
        district_id = _entity_ref_int(card.entity_ref, prefix="district")
        if district_id is None or district_id in seen:
            continue
        seen.add(district_id)
        candidates.append(
            DistrictCandidate(
                district_id=district_id,
                district_name=card.label,
                state=_metadata_str(card.metadata.get("state")),
                match_reason=_metadata_str(card.metadata.get("match_reason")),
            )
        )
    return candidates


def _profile_field_candidates_from_recall_report(
    report: "RecallReport",
) -> list[NCESFieldCandidate]:
    candidates: list[NCESFieldCandidate] = []
    seen: set[str] = set()
    for card in report.candidates:
        if (
            card.entity_type != "profile_field"
            or "profile_field_search" not in card.source_methods
        ):
            continue
        field_key = _entity_ref_key(card.entity_ref, prefix="profile_field")
        if field_key is None or field_key in seen:
            continue
        seen.add(field_key)
        candidates.append(
            NCESFieldCandidate(
                field_key=field_key,
                label=card.label,
                data_type=_metadata_str(card.metadata.get("data_type")),
                description=card.plain_definition,
            )
        )
    return candidates


def _entity_ref_int(entity_ref: str | None, *, prefix: str) -> int | None:
    value = _entity_ref_key(entity_ref, prefix=prefix)
    return _int_or_none(value) if value is not None else None


def _entity_ref_key(entity_ref: str | None, *, prefix: str) -> str | None:
    if not entity_ref:
        return None
    expected = f"{prefix}:"
    if not entity_ref.startswith(expected):
        return None
    value = entity_ref[len(expected):].strip()
    return value or None


def _metadata_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _observation_candidate_search_query(query: str) -> str | None:
    """Return a canned observation query to ADD to catalog candidate search.

    Doctrine ('No prose-dispatch below the planner', CLAUDE.md):
    this is candidate-generation — it widens the set of metrics the catalog
    adjudicator can choose from when the planner emitted a broad
    observation-family phrase. It does NOT route the request, rewrite the
    plan, or substitute a typed field. The "add, never subtract" property is
    load-bearing: the original ``MetricSpec.name`` is still searched (see
    `_metric_candidate_search_queries`); the canned query is appended, not
    substituted. If the planner emitted a specific observation metric phrase,
    the adjudicator picks it; if the phrase was broad, the adjudicator gets
    both the original and the canned query as candidates and can clarify.
    """
    tokens = set(_metric_query_tokens(query))
    if not tokens:
        return None
    if tokens & _OBSERVATION_CANDIDATE_BLOCKED_TOKENS:
        return None
    if _watching_teachers_teach(tokens):
        return _OBSERVATION_CANDIDATE_CANNED_QUERY
    if not _mentions_observation(tokens):
        return None
    if not (
        tokens & _OBSERVATION_CANDIDATE_QUANTITY_TOKENS
        or tokens & _OBSERVATION_CANDIDATE_CONTEXT_TOKENS
    ):
        return None
    return _OBSERVATION_CANDIDATE_CANNED_QUERY


def requires_observation_metric_clarification(query: str) -> bool:
    """Return whether an observation-family phrase is too broad to execute."""

    tokens = set(_metric_query_tokens(query))
    if not tokens:
        return False
    if tokens & _OBSERVATION_CANDIDATE_BLOCKED_TOKENS:
        return False
    if _mentions_teacher_status(tokens):
        return False
    if _watching_teachers_teach(tokens):
        return True
    return _mentions_observation(tokens) and bool(
        tokens & _OBSERVATION_CANDIDATE_CONTEXT_TOKENS
        or tokens & _OBSERVATION_CANDIDATE_QUANTITY_TOKENS
    )


def _clarifying_observation_metric_candidates(
    query: str,
    candidates: list[MetricCandidate],
    *,
    numeric_only: bool,
) -> list[MetricCandidate]:
    if not requires_observation_metric_clarification(query):
        return []
    observation_candidates: list[MetricCandidate] = []
    seen: set[int] = set()
    for candidate in candidates:
        if candidate.metric_id in seen:
            continue
        if numeric_only and (candidate.answer_type or "").casefold() != "numeric":
            continue
        if not _observation_metric_candidate(candidate):
            continue
        seen.add(candidate.metric_id)
        observation_candidates.append(candidate)
    if len(observation_candidates) < 2:
        return []
    return observation_candidates


def _observation_metric_candidate(candidate: MetricCandidate) -> bool:
    tokens = set(_metric_query_tokens(f"{candidate.name} {candidate.topic or ''}"))
    return _mentions_observation(tokens)


def _mentions_observation(tokens: set[str]) -> bool:
    return bool(tokens & {"observation", "observations"})


def _candidate_lane(candidate: MetricCandidate) -> "DegreeLane | None":
    """Classify a metric candidate as 'ba', 'ma', or None (lane-neutral).

    Thin wrapper over `catalog.degree_lane.classify_metric_lane`. The shared
    classifier is the single source of truth; this function stays so callers
    in `resolution.py` keep their `MetricCandidate`-shaped boundary.

    CLAUDE.md doctrine: this is a deterministic classifier, not a
    prose-dispatch router. It classifies the *candidate* (a typed object
    from the catalog), never the user's free-text message.
    """
    from compass_backend.catalog.degree_lane import classify_metric_lane

    return classify_metric_lane(candidate.name)


def _filter_candidates_by_degree_lane(
    candidates: list[MetricCandidate],
    lane: "DegreeLane",
) -> list[MetricCandidate]:
    """Filter a candidate list to those matching *lane*, keeping lane-neutral ones.

    Lane-neutral candidates (no degree token in name) are always kept — they
    may still be relevant and should not disappear silently. Only opposite-lane
    candidates are removed.

    If ALL surviving candidates have the *wrong* lane (e.g. BA alias + MA lane
    requested), returns an empty list — the caller should treat this as a
    conflict / refusal, not silently fall back to the wrong lane.

    CLAUDE.md doctrine: this is an "add-only" candidate broadener (never subtracts
    from the user's stated intent). It applies AFTER alias resolution but BEFORE
    adjudication, so it can narrow an ambiguous set to a single resolved metric.
    """
    result: list[MetricCandidate] = []
    for candidate in candidates:
        candidate_lane = _candidate_lane(candidate)
        if candidate_lane is None or candidate_lane == lane:
            result.append(candidate)
    return result


def _opposite_lane_candidates(
    candidates: list[MetricCandidate],
    lane: "DegreeLane",
) -> list[MetricCandidate]:
    """Return the candidates *dropped* by `_filter_candidates_by_degree_lane`.

    Used to populate `MetricBundleResolution.alternate_candidates` so the
    renderer can footnote "MA data is also available" when the planner
    defaulted to BA on an ambiguous salary alias.
    """
    dropped: list[MetricCandidate] = []
    for candidate in candidates:
        candidate_lane = _candidate_lane(candidate)
        if candidate_lane is not None and candidate_lane != lane:
            dropped.append(candidate)
    return dropped


def _mentions_teacher_status(tokens: set[str]) -> bool:
    if tokens & _OBSERVATION_STATUS_TOKENS:
        return True
    return "non" in tokens and "tenured" in tokens


def _watching_teachers_teach(tokens: set[str]) -> bool:
    return bool(
        tokens & _OBSERVATION_ACTION_TOKENS
        and tokens & {"teacher", "teachers"}
        and tokens & {"teach", "teaches", "teaching", "classroom"}
    )


def _metric_query_tokens(query: str) -> list[str]:
    return [
        token
        for token in _METRIC_QUERY_TOKEN_RE.sub(" ", query.casefold()).split()
        if token
    ]


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        key = _normalize_metric_name(value)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def _catalog_adjudication_candidate(
    candidate: MetricCandidate,
) -> CatalogAdjudicationCandidate:
    metadata: dict[str, Any] = dict(candidate.metadata)
    if candidate.answer_type is not None:
        metadata["answer_type"] = candidate.answer_type
    if candidate.topic is not None:
        metadata["topic"] = candidate.topic
    return CatalogAdjudicationCandidate(
        entity_type="metric",
        candidate_id=str(candidate.metric_id),
        label=candidate.name,
        metadata=metadata,
    )


def _selected_adjudicated_metrics(
    decision: CatalogAdjudicationDecision,
    candidates: list[MetricCandidate],
    *,
    numeric_only: bool,
) -> list[MetricCandidate]:
    candidates_by_id = {
        str(candidate.metric_id): candidate for candidate in candidates
    }
    selected: list[MetricCandidate] = []
    for selected_id in decision.selected_ids:
        candidate = candidates_by_id.get(selected_id)
        if candidate is None:
            return []
        if numeric_only and (candidate.answer_type or "").casefold() != "numeric":
            return []
        selected.append(candidate)
    return selected


def _district_candidates_from_aliases(
    aliases: list[CatalogAliasRecord],
) -> list[DistrictCandidate]:
    seen: set[int] = set()
    candidates: list[DistrictCandidate] = []
    for alias in aliases:
        for raw in alias.candidate_refs:
            district = _district_candidate_from_ref(raw)
            if district is None or district.district_id in seen:
                continue
            seen.add(district.district_id)
            candidates.append(district)
    return candidates


def _district_candidate_from_ref(raw: dict[str, Any]) -> DistrictCandidate | None:
    district_id = _int_or_none(raw.get("district_id"))
    district_name = raw.get("district_name")
    if district_id is None or not isinstance(district_name, str):
        return None
    state = raw.get("state")
    return DistrictCandidate(
        district_id=district_id,
        district_name=district_name,
        state=state if isinstance(state, str) else None,
        city=raw.get("city") if isinstance(raw.get("city"), str) else None,
        county_name=(
            raw.get("county_name")
            if isinstance(raw.get("county_name"), str)
            else None
        ),
        enrollment=_int_or_none(raw.get("enrollment")),
        locale_type=(
            raw.get("locale_type")
            if isinstance(raw.get("locale_type"), str)
            else None
        ),
    )


def _metric_ids_for_alias(alias: CatalogAliasRecord) -> list[int]:
    values = alias.canonical_ids or (
        [alias.canonical_id] if alias.canonical_id else []
    )
    ids = [_int_or_none(value) for value in values]
    return [value for value in ids if value is not None]


def _candidate_metric_ids_for_alias(alias: CatalogAliasRecord) -> list[int]:
    ids: list[int] = []
    for raw in alias.candidate_refs:
        metric_id = _int_or_none(raw.get("metric_id"))
        if metric_id is not None and metric_id not in ids:
            ids.append(metric_id)
    return ids


def _contextual_default_payload(
    alias: CatalogAliasRecord,
    context_key: str,
) -> dict[str, Any] | None:
    defaults = alias.metadata.get("contextual_defaults")
    if not isinstance(defaults, dict):
        return None
    payload = defaults.get(context_key)
    return payload if isinstance(payload, dict) else None


def _alias_authorizes_metric_id(alias: CatalogAliasRecord, metric_id: int) -> bool:
    return metric_id in {
        *_metric_ids_for_alias(alias),
        *_candidate_metric_ids_for_alias(alias),
    }


def _ordered_alias_metrics(
    metric_ids: list[int],
    metrics: list[MetricCandidate],
    *,
    numeric_only: bool,
) -> list[MetricCandidate]:
    resolved_by_id = {metric.metric_id: metric for metric in metrics}
    ordered: list[MetricCandidate] = []
    for metric_id in metric_ids:
        metric = resolved_by_id.get(metric_id)
        if metric is None:
            continue
        if numeric_only and (metric.answer_type or "").casefold() != "numeric":
            continue
        ordered.append(metric)
    return ordered


def _metric_name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _alias_bundle_key(alias: CatalogAliasRecord) -> str:
    return re.sub(r"[^a-z0-9]+", "_", alias.normalized_alias).strip("_")


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
