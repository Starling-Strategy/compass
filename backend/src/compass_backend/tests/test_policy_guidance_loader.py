"""Tests for the markdown -> typed library loader."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from compass_backend.policy_guidance.loader import (
    LibraryLoadError,
    load_library,
    parse_topic_file,
)

# A minimal happy-path topic: one stance, one ready rationale, one exemplar.
HAPPY_TOPIC_MD = dedent(
    """\
    ---
    topic_id: general-salary
    canonical_topic: General Salary
    publish_status: ready_to_proofread
    canonical_url:
    aliases:
      - salary
      - salary schedule
      - starting salary
    source_tables:
      rationales: compass.nctq_rationales
      exemplars: compass.nctq_exemplar_policies
    ---

    # General Salary

    ## Topic Brief

    Districts set teacher pay; structures vary widely by step and lane.

    ## Stances

    ### [stance:general-salary-frontloaded-schedules] Frontloaded Schedules

    Districts should frontload teacher pay to retain early-career teachers.

    ## Research Rationales

    ### [rationale:general-salary-frontloaded-schedules] Frontloaded Schedules

    - stance_id: stance:general-salary-frontloaded-schedules
    - source_id: source:nctq-research-general-salary
    - source_title: NCTQ Research
    - source_url: https://www.nctq.org/district-policy-pathfinder/
    - citation_status: placeholder

    Teacher attrition is highest in the first five years.

    ## Exemplary Policies

    ### [exemplar:detroit-frontloaded-salary-schedule] Detroit Community School District

    - district: Detroit Community School District
    - district_id:
    - subtopic: Annual salary
    - source_url: https://teacherquality.nctq.org/contract-database/district/detroit
    - citation_status: ready

    Detroit restructured its salary schedule to provide substantial pay
    increases within the first five years.
    """
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# Happy path


def test_parse_topic_file_happy_path(tmp_path: Path) -> None:
    p = _write(tmp_path / "general-salary.md", HAPPY_TOPIC_MD)
    topic = parse_topic_file(p)

    assert topic.topic_id == "general-salary"
    assert topic.canonical_topic == "General Salary"
    assert topic.aliases == ("salary", "salary schedule", "starting salary")
    assert topic.canonical_url is None
    assert "step and lane" in topic.topic_brief

    assert len(topic.stances) == 1
    assert topic.stances[0].stance_id == "stance:general-salary-frontloaded-schedules"
    assert topic.stances[0].title == "Frontloaded Schedules"
    assert "frontload" in topic.stances[0].body.lower()

    assert len(topic.rationales) == 1
    rationale = topic.rationales[0]
    assert rationale.rationale_id == "rationale:general-salary-frontloaded-schedules"
    assert rationale.stance_id == "stance:general-salary-frontloaded-schedules"
    assert rationale.source_title == "NCTQ Research"
    assert rationale.citation_status == "placeholder"
    assert "first five years" in rationale.body

    assert len(topic.exemplars) == 1
    exemplar = topic.exemplars[0]
    assert exemplar.exemplar_id == "exemplar:detroit-frontloaded-salary-schedule"
    assert exemplar.district == "Detroit Community School District"
    assert exemplar.district_id is None
    # No `- state:` bullet → state stays absent (backward compatible).
    assert exemplar.state is None
    assert exemplar.subtopic == "Annual salary"
    assert exemplar.citation_status == "ready"


def test_load_library_from_directory(tmp_path: Path) -> None:
    _write(tmp_path / "general-salary.md", HAPPY_TOPIC_MD)
    leave_md = HAPPY_TOPIC_MD.replace(
        "topic_id: general-salary", "topic_id: leave"
    ).replace(
        "canonical_topic: General Salary", "canonical_topic: Leave"
    ).replace(
        "general-salary-frontloaded-schedules", "leave-paid-parental"
    ).replace(
        "Frontloaded Schedules", "Paid Parental"
    ).replace(
        "detroit-frontloaded-salary-schedule", "anchorage-parental-leave"
    )
    _write(tmp_path / "leave.md", leave_md)

    library = load_library(tmp_path)
    assert set(library.all_topic_ids) == {"general-salary", "leave"}
    assert "stance:leave-paid-parental" in library.all_stance_ids


# ---------------------------------------------------------------------------
# Strict validation — all unhappy paths


def test_loader_rejects_rationale_with_missing_source_url(tmp_path: Path) -> None:
    """Pydantic's HttpUrl is required — an empty string should fail."""
    bad = HAPPY_TOPIC_MD.replace(
        "- source_url: https://www.nctq.org/district-policy-pathfinder/",
        "- source_url:",
    )
    p = _write(tmp_path / "general-salary.md", bad)
    with pytest.raises((LibraryLoadError, ValidationError)):
        parse_topic_file(p)


def test_loader_rejects_rationale_referencing_unknown_stance(tmp_path: Path) -> None:
    bad = HAPPY_TOPIC_MD.replace(
        "- stance_id: stance:general-salary-frontloaded-schedules",
        "- stance_id: stance:does-not-exist",
    )
    p = _write(tmp_path / "general-salary.md", bad)
    with pytest.raises((LibraryLoadError, ValidationError), match="unknown stance"):
        parse_topic_file(p)


def test_loader_rejects_needs_source_url_status(tmp_path: Path) -> None:
    """Active library refuses the historical-only needs_source_url state."""
    bad = HAPPY_TOPIC_MD.replace(
        "- citation_status: placeholder",
        "- citation_status: needs_source_url",
    )
    p = _write(tmp_path / "general-salary.md", bad)
    with pytest.raises((LibraryLoadError, ValidationError)):
        parse_topic_file(p)


def test_loader_rejects_placeholder_with_real_url(tmp_path: Path) -> None:
    """Drift catcher: status='placeholder' but URL isn't the homepage."""
    bad = HAPPY_TOPIC_MD.replace(
        "- source_url: https://www.nctq.org/district-policy-pathfinder/",
        "- source_url: https://www.nctq.org/some-real-page",
    )
    p = _write(tmp_path / "general-salary.md", bad)
    with pytest.raises((LibraryLoadError, ValidationError)):
        parse_topic_file(p)


def test_loader_rejects_duplicate_stable_id_across_topics(tmp_path: Path) -> None:
    """Stable IDs must be globally unique to be safe as Literal[*] enums."""
    _write(tmp_path / "topic1.md", HAPPY_TOPIC_MD)
    # Second file uses a different topic_id but same stance_id — should fail
    duplicate = HAPPY_TOPIC_MD.replace(
        "topic_id: general-salary", "topic_id: leave"
    ).replace(
        "canonical_topic: General Salary", "canonical_topic: Leave"
    )
    _write(tmp_path / "leave.md", duplicate)
    with pytest.raises((LibraryLoadError, ValueError), match="duplicate"):
        load_library(tmp_path)


def test_loader_rejects_missing_required_frontmatter(tmp_path: Path) -> None:
    bad = HAPPY_TOPIC_MD.replace("topic_id: general-salary\n", "")
    p = _write(tmp_path / "general-salary.md", bad)
    with pytest.raises((LibraryLoadError, KeyError, ValidationError)):
        parse_topic_file(p)


def test_loader_rejects_unparseable_frontmatter(tmp_path: Path) -> None:
    bad = "not a frontmatter\n\n# Body\n"
    p = _write(tmp_path / "general-salary.md", bad)
    with pytest.raises(LibraryLoadError):
        parse_topic_file(p)


# ---------------------------------------------------------------------------
# Real content sanity check (skipped in unit run; gated by file presence)


def test_loader_skips_redirect_stub_files(tmp_path: Path) -> None:
    """Files with `redirect_to:` frontmatter are documentation stubs (no body
    sections) — loader should silently skip them."""
    redirect_md = dedent(
        """\
        ---
        topic_id: performance-pay
        canonical_topic: Performance Pay
        redirect_to: differentiated-pay
        aliases:
          - merit pay
          - performance bonus
        ---

        # Performance Pay

        Performance pay is modeled as a subtopic of differentiated pay.
        Use differentiated-pay's stable IDs.
        """
    )
    _write(tmp_path / "performance-pay.md", redirect_md)
    _write(tmp_path / "general-salary.md", HAPPY_TOPIC_MD)

    library = load_library(tmp_path)
    assert "general-salary" in library.topics
    assert "performance-pay" not in library.topics


def test_load_real_content_directory_smoke() -> None:
    """If the real content dir exists, it should load cleanly with the strict
    invariants. Loud failure here means a content-author edit broke something
    and CI must catch it before staging deploy."""
    real = Path(__file__).resolve().parents[3] / "content" / "nctq-policy" / "topics"
    if not real.exists():
        pytest.skip("real content dir not present in this checkout")

    library = load_library(real)
    # 8 .md files but performance-pay.md is a redirect stub → 7 loaded topics
    assert len(library.topics) == 7
    assert "performance-pay" not in library.topics
    # There are 30 active rationales and 30 exemplars per .context plan
    total_rationales = sum(len(t.rationales) for t in library.topics.values())
    total_exemplars = sum(len(t.exemplars) for t in library.topics.values())
    assert total_rationales == 30, f"expected 30 rationales across topics, got {total_rationales}"
    assert total_exemplars == 30, f"expected 30 exemplars across topics, got {total_exemplars}"
    # Detroit must be present and frontloaded
    detroit = [
        e
        for t in library.topics.values()
        for e in t.exemplars
        if e.exemplar_id == "exemplar:detroit-frontloaded-salary-schedule"
    ]
    assert len(detroit) == 1
    assert "first five years" in detroit[0].body


def test_exemplar_state_bullet_parses_and_normalizes(tmp_path: Path) -> None:
    """A `- state: tx` bullet is parsed and normalized to an uppercase code
    (#1360 — governed regional refinement of exemplars)."""
    # HAPPY_TOPIC_MD is dedent-ed, so bullets sit at column 0. `- district_id:`
    # appears once (only the exemplar carries it), so inserting after it is safe.
    md = HAPPY_TOPIC_MD.replace(
        "- district_id:\n",
        "- district_id:\n- state: mi\n",
    )
    topic = parse_topic_file(_write(tmp_path / "general-salary.md", md))
    assert topic.exemplars[0].state == "MI"
