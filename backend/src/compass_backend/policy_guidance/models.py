"""Pydantic models for the NCTQ policy-guidance content surface.

The library is loaded once at FastAPI startup from
``content/nctq-policy/topics/*.md`` and stays immutable for the process
lifetime. Stable IDs (``stance:slug``, ``rationale:slug``, ``exemplar:slug``)
are globally unique and act as citation anchors in rendered responses.

Citation status is intentionally binary at runtime: ``ready`` means a real
public source URL; ``placeholder`` means the URL is the Pathfinder homepage
while the topic-anchored page hasn't launched yet. The historical
``needs_source_url`` value from the markdown stopgap is unrepresentable here
on purpose — every rationale must have *some* URL the user can click.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

CitationStatus = Literal["ready", "placeholder"]
GuidanceLayer = Literal["stances", "rationales", "exemplars"]
StanceKind = Literal["policy_stance", "no_policy_stance"]
PATHFINDER_PLACEHOLDER = "https://www.nctq.org/district-policy-pathfinder/"


def _is_placeholder_url(url: AnyHttpUrl | str) -> bool:
    return str(url).rstrip("/") == PATHFINDER_PLACEHOLDER.rstrip("/")


class _Frozen(BaseModel):
    """Shared base config: frozen + reject unexpected keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Stance(_Frozen):
    """A short NCTQ policy position. No own URL — cites via topic canonical_url."""

    stance_id: str = Field(min_length=1)
    topic_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    kind: StanceKind = "policy_stance"

    @model_validator(mode="after")
    def _infer_kind_from_id(self) -> "Stance":
        # Deterministic discriminator: stance_id ending in "-no-policy-stance"
        # signals NCTQ's chosen non-position. This is the existing content
        # convention; promote it to a typed field so the renderer can route.
        # Use object.__setattr__ because Stance inherits frozen=True from _Frozen.
        if self.stance_id.endswith("-no-policy-stance"):
            object.__setattr__(self, "kind", "no_policy_stance")
        return self


class ResearchRationale(_Frozen):
    """The 'why' behind a stance. Must have a clickable source URL at all times."""

    rationale_id: str = Field(min_length=1)
    stance_id: str = Field(min_length=1)
    topic_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    source_title: str = Field(min_length=1)
    source_url: AnyHttpUrl
    citation_status: CitationStatus

    @model_validator(mode="after")
    def url_matches_status(self) -> Self:
        is_placeholder = _is_placeholder_url(self.source_url)
        if self.citation_status == "placeholder" and not is_placeholder:
            raise ValueError(
                f"placeholder rationale {self.rationale_id} must use the "
                f"Pathfinder homepage URL ({PATHFINDER_PLACEHOLDER})"
            )
        if self.citation_status == "ready" and is_placeholder:
            raise ValueError(
                f"ready rationale {self.rationale_id} cannot use the homepage "
                f"placeholder URL — set citation_status='placeholder' or supply "
                f"a real source URL"
            )
        return self


class ExemplarPolicy(_Frozen):
    """A specific district NCTQ holds up as a model for a topic."""

    exemplar_id: str = Field(min_length=1)
    topic_id: str = Field(min_length=1)
    district: str = Field(min_length=1)
    district_id: int | None = None  # filled when linked to compass.district_profiles
    # Two-letter USPS state code (e.g. "TX", "DC"). Optional because some
    # exemplars are state-level entities (e.g. "Hawaii Department of Education")
    # or predate the field. The renderer uses it to apply governed regional
    # refinements ("narrow to the South") without re-deriving geography from the
    # district name; an active region filter drops unknown-state exemplars
    # rather than guessing.
    state: str | None = None
    subtopic: str = Field(min_length=1)
    body: str = Field(min_length=1)
    source_url: AnyHttpUrl
    citation_status: CitationStatus

    @field_validator("state", mode="before")
    @classmethod
    def _normalize_state(cls, value: object) -> str | None:
        """Uppercase + trim a 2-letter state code; treat blank as absent."""
        if value is None:
            return None
        text = str(value).strip().upper()
        return text or None

    @model_validator(mode="after")
    def url_matches_status(self) -> Self:
        is_placeholder = _is_placeholder_url(self.source_url)
        if self.citation_status == "placeholder" and not is_placeholder:
            raise ValueError(
                f"placeholder exemplar {self.exemplar_id} must use the "
                f"Pathfinder homepage URL ({PATHFINDER_PLACEHOLDER})"
            )
        if self.citation_status == "ready" and is_placeholder:
            raise ValueError(
                f"ready exemplar {self.exemplar_id} cannot use the homepage "
                f"placeholder URL"
            )
        return self


class TopicGuidance(_Frozen):
    """All guidance for a single NCTQ topic."""

    topic_id: str = Field(min_length=1)
    canonical_topic: str = Field(min_length=1)
    aliases: tuple[str, ...]
    canonical_url: AnyHttpUrl | None
    topic_brief: str = Field(min_length=1)
    stances: tuple[Stance, ...]
    rationales: tuple[ResearchRationale, ...]
    exemplars: tuple[ExemplarPolicy, ...]

    @model_validator(mode="after")
    def rationale_stance_refs_resolve(self) -> Self:
        stance_ids = {s.stance_id for s in self.stances}
        for rationale in self.rationales:
            if rationale.stance_id not in stance_ids:
                raise ValueError(
                    f"rationale {rationale.rationale_id} references unknown stance "
                    f"{rationale.stance_id} in topic {self.topic_id}"
                )
        return self


class PolicyGuidanceBundle(_Frozen):
    """The subset of the library a single response surface needs."""

    topic_ids: tuple[str, ...]
    layers: tuple[GuidanceLayer, ...]
    stances: tuple[Stance, ...]
    rationales: tuple[ResearchRationale, ...]
    exemplars: tuple[ExemplarPolicy, ...]


class PolicyGuidanceLibrary(_Frozen):
    """Immutable, process-lifetime collection of every topic's guidance.

    The derived ``all_*_ids`` tuples are the source of truth for any
    Literal[*] runtime constraints (planner topic enums, citation-marker
    validators, etc.).
    """

    topics: dict[str, TopicGuidance]
    all_topic_ids: tuple[str, ...]
    canonical_topics: tuple[str, ...]
    all_stance_ids: tuple[str, ...]
    all_rationale_ids: tuple[str, ...]
    all_exemplar_ids: tuple[str, ...]

    @classmethod
    def build(cls, *, topics: dict[str, TopicGuidance]) -> "PolicyGuidanceLibrary":
        """Construct a library from per-topic guidance and derive the ID indexes.

        Enforces global uniqueness of stable IDs across topics — needed because
        Literal[*] runtime enums and citation lookups assume a flat ID space.
        """

        all_topic_ids = tuple(topics.keys())
        canonical_topics = tuple(t.canonical_topic for t in topics.values())

        stance_ids: list[str] = []
        rationale_ids: list[str] = []
        exemplar_ids: list[str] = []
        for topic in topics.values():
            stance_ids.extend(s.stance_id for s in topic.stances)
            rationale_ids.extend(r.rationale_id for r in topic.rationales)
            exemplar_ids.extend(e.exemplar_id for e in topic.exemplars)

        cls._reject_duplicates("stance", stance_ids)
        cls._reject_duplicates("rationale", rationale_ids)
        cls._reject_duplicates("exemplar", exemplar_ids)

        return cls(
            topics=topics,
            all_topic_ids=all_topic_ids,
            canonical_topics=canonical_topics,
            all_stance_ids=tuple(stance_ids),
            all_rationale_ids=tuple(rationale_ids),
            all_exemplar_ids=tuple(exemplar_ids),
        )

    @staticmethod
    def _reject_duplicates(kind: str, ids: list[str]) -> None:
        seen: set[str] = set()
        for sid in ids:
            if sid in seen:
                raise ValueError(
                    f"duplicate {kind} id {sid!r} across topics — stable IDs "
                    f"must be globally unique"
                )
            seen.add(sid)

    def assemble(
        self,
        *,
        topic_ids: list[str],
        layers: list[GuidanceLayer],
    ) -> PolicyGuidanceBundle:
        """Return only the requested layers across the requested topics."""

        unknown = [tid for tid in topic_ids if tid not in self.topics]
        if unknown:
            raise ValueError(f"unknown topic ids: {unknown!r}")

        wants = set(layers)
        stances: list[Stance] = []
        rationales: list[ResearchRationale] = []
        exemplars: list[ExemplarPolicy] = []
        for tid in topic_ids:
            topic = self.topics[tid]
            if "stances" in wants:
                stances.extend(topic.stances)
            if "rationales" in wants:
                rationales.extend(topic.rationales)
            if "exemplars" in wants:
                exemplars.extend(topic.exemplars)

        return PolicyGuidanceBundle(
            topic_ids=tuple(topic_ids),
            layers=tuple(layers),
            stances=tuple(stances),
            rationales=tuple(rationales),
            exemplars=tuple(exemplars),
        )
