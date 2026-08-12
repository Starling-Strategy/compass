"""Tests for the process-lifetime PolicyGuidanceLibrary singleton."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from compass_backend.policy_guidance import (
    PolicyGuidanceLibrary,
    Stance,
    TopicGuidance,
)
from compass_backend.policy_guidance.library import (
    LibraryNotLoadedError,
    get_library,
    load_default_library,
    reset_library,
    set_library,
)
from compass_backend.policy_guidance.loader import load_library


@pytest.fixture(autouse=True)
def _reset_library_state():
    """Each test starts with a fresh empty singleton."""
    reset_library()
    yield
    reset_library()


def _toy_library() -> PolicyGuidanceLibrary:
    stance = Stance(
        stance_id="stance:test-x",
        topic_id="test",
        title="Test Stance",
        body="Test body.",
    )
    topic = TopicGuidance(
        topic_id="test",
        canonical_topic="Test",
        aliases=("test",),
        canonical_url=None,
        topic_brief="Brief.",
        stances=(stance,),
        rationales=(),
        exemplars=(),
    )
    return PolicyGuidanceLibrary.build(topics={"test": topic})


def test_get_library_before_set_raises() -> None:
    """Backend code that depends on the library should fail loudly if startup
    didn't load it — better than silently returning None."""
    with pytest.raises(LibraryNotLoadedError):
        get_library()


def test_set_then_get_round_trip() -> None:
    lib = _toy_library()
    set_library(lib)
    assert get_library() is lib  # exact identity — same process-lifetime object


def test_reset_library_clears_state() -> None:
    set_library(_toy_library())
    reset_library()
    with pytest.raises(LibraryNotLoadedError):
        get_library()


def test_set_library_replaces_previous() -> None:
    """A second set_library call replaces the first (defensible if startup
    is re-run; not surprising)."""
    set_library(_toy_library())
    second = _toy_library()
    set_library(second)
    assert get_library() is second


def test_load_default_library_uses_real_content_dir() -> None:
    """The lifespan hook should load against the bundled content/ markdown."""
    load_default_library()
    library = get_library()
    assert len(library.topics) == 7  # performance-pay is a redirect stub
    assert "general-salary" in library.topics


def test_load_default_library_with_explicit_dir(tmp_path: Path) -> None:
    """The hook accepts an explicit content_dir for tests + non-default deployments."""
    topic_md = dedent(
        """\
        ---
        topic_id: smoke
        canonical_topic: Smoke
        aliases:
          - smoke
        ---

        # Smoke

        ## Topic Brief

        Smoke test topic.

        ## Stances

        ### [stance:smoke-x] Smoke X

        Smoke stance body.
        """
    )
    (tmp_path / "smoke.md").write_text(topic_md)
    load_default_library(content_dir=tmp_path)
    library = get_library()
    assert "smoke" in library.topics


def test_load_default_library_loud_on_broken_content(tmp_path: Path) -> None:
    """Lifespan should refuse to boot if content fails strict validation."""
    bad_md = dedent(
        """\
        ---
        topic_id: bad
        canonical_topic: Bad
        ---

        # Bad

        ## Topic Brief

        Brief.

        ## Research Rationales

        ### [rationale:bad-no-source] Bad

        - stance_id: stance:nonexistent
        - source_title: NCTQ Research
        - source_url:
        - citation_status: needs_source_url

        Body.
        """
    )
    (tmp_path / "bad.md").write_text(bad_md)
    with pytest.raises(Exception):  # LibraryLoadError or ValidationError
        load_default_library(content_dir=tmp_path)
    # And the singleton should remain unloaded after the failure
    with pytest.raises(LibraryNotLoadedError):
        get_library()


def test_loaded_library_passes_to_consumers() -> None:
    """Consumers (planner, renderer) call get_library() — verify the same
    object the loader returned is what they receive."""
    direct = load_library()
    set_library(direct)
    via_singleton = get_library()
    assert via_singleton is direct
    assert via_singleton.all_topic_ids == direct.all_topic_ids
