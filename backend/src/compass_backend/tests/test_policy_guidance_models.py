"""Tests for the typed NCTQ policy-guidance models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compass_backend.policy_guidance.models import (
    PATHFINDER_PLACEHOLDER,
    ExemplarPolicy,
    PolicyGuidanceBundle,
    PolicyGuidanceLibrary,
    ResearchRationale,
    Stance,
    TopicGuidance,
)

# ---------------------------------------------------------------------------
# Stance


def test_stance_round_trip() -> None:
    s = Stance(
        stance_id="stance:general-salary-frontloaded-schedules",
        topic_id="general-salary",
        title="Frontloaded Schedules",
        body="Districts should frontload teacher pay to retain early-career teachers.",
    )
    assert s.stance_id == "stance:general-salary-frontloaded-schedules"
    assert s.topic_id == "general-salary"


def test_stance_is_frozen() -> None:
    s = Stance(
        stance_id="stance:x", topic_id="x", title="t", body="b",
    )
    with pytest.raises(ValidationError):
        s.title = "mutated"  # type: ignore[misc]


def test_stance_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Stance(  # type: ignore[call-arg]
            stance_id="stance:x", topic_id="x", title="t", body="b",
            unexpected="bad",
        )


# ---------------------------------------------------------------------------
# ResearchRationale + url_matches_status validator


def _ready_rationale(**overrides: object) -> ResearchRationale:
    base: dict[str, object] = {
        "rationale_id": "rationale:general-salary-frontloaded-schedules",
        "stance_id": "stance:general-salary-frontloaded-schedules",
        "topic_id": "general-salary",
        "title": "Frontloaded Schedules",
        "body": "Teacher attrition is highest in the first five years.",
        "source_title": "Beginning Teacher Longitudinal Study",
        "source_url": "https://nces.ed.gov/pubsearch/pubsinfo.asp?pubid=2015196",
        "citation_status": "ready",
    }
    base.update(overrides)
    return ResearchRationale(**base)  # type: ignore[arg-type]


def _placeholder_rationale(**overrides: object) -> ResearchRationale:
    base: dict[str, object] = {
        "rationale_id": "rationale:general-salary-competitive-pay",
        "stance_id": "stance:general-salary-competitive-pay",
        "topic_id": "general-salary",
        "title": "Competitive Pay",
        "body": "A $5,000 increase in starting salaries is associated with a 16% decrease in vacancies.",
        "source_title": "NCTQ Research",
        "source_url": PATHFINDER_PLACEHOLDER,
        "citation_status": "placeholder",
    }
    base.update(overrides)
    return ResearchRationale(**base)  # type: ignore[arg-type]


def test_rationale_ready_with_real_url_ok() -> None:
    r = _ready_rationale()
    assert r.citation_status == "ready"


def test_rationale_placeholder_with_homepage_url_ok() -> None:
    r = _placeholder_rationale()
    assert r.citation_status == "placeholder"
    assert str(r.source_url).rstrip("/") == PATHFINDER_PLACEHOLDER.rstrip("/")


def test_rationale_placeholder_with_non_homepage_url_rejected() -> None:
    """A 'placeholder' citation must use exactly the Pathfinder homepage URL."""
    with pytest.raises(ValidationError, match="placeholder"):
        _placeholder_rationale(source_url="https://www.nctq.org/some-other-page")


def test_rationale_ready_with_homepage_url_rejected() -> None:
    """A 'ready' citation must NOT use the homepage placeholder URL — drift catcher."""
    with pytest.raises(ValidationError, match="ready"):
        _ready_rationale(source_url=PATHFINDER_PLACEHOLDER)


def test_rationale_needs_source_url_status_rejected() -> None:
    """needs_source_url is intentionally not in the active Literal."""
    with pytest.raises(ValidationError):
        _ready_rationale(citation_status="needs_source_url")


def test_rationale_missing_source_url_rejected() -> None:
    """source_url is required (HttpUrl, no nullable)."""
    with pytest.raises(ValidationError):
        ResearchRationale(  # type: ignore[call-arg]
            rationale_id="rationale:x",
            stance_id="stance:x",
            topic_id="x",
            title="t",
            body="b",
            source_title="s",
            citation_status="ready",
        )


def test_rationale_invalid_url_rejected() -> None:
    with pytest.raises(ValidationError):
        _ready_rationale(source_url="not-a-url")


# ---------------------------------------------------------------------------
# ExemplarPolicy


def _exemplar(**overrides: object) -> ExemplarPolicy:
    base: dict[str, object] = {
        "exemplar_id": "exemplar:detroit-frontloaded-salary-schedule",
        "topic_id": "general-salary",
        "district": "Detroit Community School District",
        "district_id": None,
        "subtopic": "Annual salary",
        "body": "Detroit restructured its salary schedule to provide substantial pay increases within the first five years.",
        "source_url": "https://teacherquality.nctq.org/contract-database/district/detroit",
        "citation_status": "ready",
    }
    base.update(overrides)
    return ExemplarPolicy(**base)  # type: ignore[arg-type]


def test_exemplar_round_trip() -> None:
    e = _exemplar()
    assert e.district == "Detroit Community School District"
    assert e.district_id is None  # not yet linked to compass.district_profiles


def test_exemplar_district_id_optional_int() -> None:
    e = _exemplar(district_id=42)
    assert e.district_id == 42


def test_exemplar_invalid_url_rejected() -> None:
    with pytest.raises(ValidationError):
        _exemplar(source_url="ftp://nope")


# ---------------------------------------------------------------------------
# TopicGuidance — cross-record invariants


def _topic(
    *,
    rationales: tuple[ResearchRationale, ...] = (),
    stances: tuple[Stance, ...] = (),
    exemplars: tuple[ExemplarPolicy, ...] = (),
) -> TopicGuidance:
    return TopicGuidance(
        topic_id="general-salary",
        canonical_topic="General Salary",
        aliases=("salary", "salary schedule", "starting salary", "teacher pay"),
        canonical_url=None,
        topic_brief="Teachers earn higher salaries via experience and education.",
        stances=stances,
        rationales=rationales,
        exemplars=exemplars,
    )


def test_topic_with_resolved_stance_ref_ok() -> None:
    stance = Stance(
        stance_id="stance:general-salary-frontloaded-schedules",
        topic_id="general-salary",
        title="Frontloaded",
        body="b",
    )
    rationale = _ready_rationale()
    t = _topic(stances=(stance,), rationales=(rationale,))
    assert len(t.stances) == 1
    assert len(t.rationales) == 1


def test_topic_with_unresolved_stance_ref_rejected() -> None:
    """A rationale referencing a stance_id that doesn't exist in this topic must fail."""
    rationale = _ready_rationale(stance_id="stance:nonexistent")
    with pytest.raises(ValidationError, match="unknown stance"):
        _topic(rationales=(rationale,))


def test_topic_aliases_is_tuple_not_list() -> None:
    """Aliases must be hashable/frozen for safe sharing."""
    t = _topic()
    assert isinstance(t.aliases, tuple)


# ---------------------------------------------------------------------------
# PolicyGuidanceLibrary + assemble


def _library_with_two_topics() -> PolicyGuidanceLibrary:
    salary_stance = Stance(
        stance_id="stance:general-salary-frontloaded-schedules",
        topic_id="general-salary",
        title="Frontloaded Schedules",
        body="b",
    )
    salary_rationale = _ready_rationale()
    salary_exemplar = _exemplar()
    salary = TopicGuidance(
        topic_id="general-salary",
        canonical_topic="General Salary",
        aliases=("salary",),
        canonical_url=None,
        topic_brief="brief",
        stances=(salary_stance,),
        rationales=(salary_rationale,),
        exemplars=(salary_exemplar,),
    )
    leave_stance = Stance(
        stance_id="stance:leave-paid-parental",
        topic_id="leave",
        title="Paid Parental Leave",
        body="b",
    )
    leave = TopicGuidance(
        topic_id="leave",
        canonical_topic="Leave",
        aliases=("leave", "parental leave"),
        canonical_url=None,
        topic_brief="brief",
        stances=(leave_stance,),
        rationales=(),
        exemplars=(),
    )
    return PolicyGuidanceLibrary.build(topics={"general-salary": salary, "leave": leave})


def test_library_derives_topic_id_tuple() -> None:
    lib = _library_with_two_topics()
    assert lib.all_topic_ids == ("general-salary", "leave")


def test_library_derives_canonical_topics() -> None:
    lib = _library_with_two_topics()
    assert lib.canonical_topics == ("General Salary", "Leave")


def test_library_derives_stable_id_indexes() -> None:
    lib = _library_with_two_topics()
    assert "stance:general-salary-frontloaded-schedules" in lib.all_stance_ids
    assert "stance:leave-paid-parental" in lib.all_stance_ids
    assert "rationale:general-salary-frontloaded-schedules" in lib.all_rationale_ids
    assert "exemplar:detroit-frontloaded-salary-schedule" in lib.all_exemplar_ids


def test_library_assemble_filters_by_topic_and_layer() -> None:
    lib = _library_with_two_topics()
    bundle = lib.assemble(
        topic_ids=["general-salary"], layers=["exemplars"],
    )
    assert isinstance(bundle, PolicyGuidanceBundle)
    assert len(bundle.exemplars) == 1
    assert bundle.exemplars[0].district == "Detroit Community School District"
    assert bundle.stances == ()
    assert bundle.rationales == ()


def test_library_assemble_multi_topic_multi_layer() -> None:
    lib = _library_with_two_topics()
    bundle = lib.assemble(
        topic_ids=["general-salary", "leave"],
        layers=["stances", "exemplars"],
    )
    assert len(bundle.stances) == 2  # one per topic
    assert len(bundle.exemplars) == 1  # only salary topic has an exemplar in the fixture
    assert bundle.rationales == ()


def test_library_assemble_unknown_topic_rejected() -> None:
    lib = _library_with_two_topics()
    with pytest.raises(ValueError, match="unknown topic"):
        lib.assemble(topic_ids=["nonexistent"], layers=["stances"])


def test_library_build_rejects_duplicate_stance_ids_across_topics() -> None:
    """Stable IDs must be globally unique to be safe as Literal[*] enums."""
    salary_stance = Stance(
        stance_id="stance:duplicate", topic_id="general-salary", title="t", body="b",
    )
    leave_stance = Stance(
        stance_id="stance:duplicate", topic_id="leave", title="t", body="b",
    )
    salary = TopicGuidance(
        topic_id="general-salary", canonical_topic="General Salary",
        aliases=("salary",), canonical_url=None, topic_brief="b",
        stances=(salary_stance,), rationales=(), exemplars=(),
    )
    leave = TopicGuidance(
        topic_id="leave", canonical_topic="Leave",
        aliases=("leave",), canonical_url=None, topic_brief="b",
        stances=(leave_stance,), rationales=(), exemplars=(),
    )
    with pytest.raises(ValueError, match="duplicate"):
        PolicyGuidanceLibrary.build(topics={"general-salary": salary, "leave": leave})
