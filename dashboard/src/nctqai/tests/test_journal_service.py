"""Tests for the Journal lesson model + service (file parsing, no DB)."""
from __future__ import annotations

import textwrap
from pathlib import Path

from nctqai.models.journal import Lesson


def _write(d: Path, name: str, text: str) -> None:
    (d / name).write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")


def test_lesson_model_allows_missing_issue_and_pr():
    lesson = Lesson(slug="x", title="T")
    assert lesson.issue is None
    assert lesson.pr is None
    assert lesson.tags == []
    assert lesson.concept == ""


from nctqai.services.journal import get_lesson, list_lessons


def test_list_lessons_sorts_newest_first(tmp_path):
    _write(tmp_path, "a.md", """
        ---
        title: "Older"
        date: 2026-05-01
        hook: "old"
        ---
        body a
    """)
    _write(tmp_path, "b.md", """
        ---
        title: "Newer"
        date: 2026-06-01
        hook: "new"
        ---
        body b
    """)
    lessons = list_lessons(lessons_dir=tmp_path)
    assert [l.title for l in lessons] == ["Newer", "Older"]


def test_list_lessons_excludes_readme_and_template(tmp_path):
    _write(tmp_path, "README.md", "# index\n")
    _write(tmp_path, "_TEMPLATE.md", "---\ntitle: T\n---\nx")
    _write(tmp_path, "real.md", """
        ---
        title: "Real"
        date: 2026-06-01
        ---
        body
    """)
    lessons = list_lessons(lessons_dir=tmp_path)
    assert [l.slug for l in lessons] == ["real"]


def test_list_lessons_parses_lesson_without_issue(tmp_path):
    _write(tmp_path, "data-fidelity.md", """
        ---
        title: "Why Compass never makes up a number"
        date: 2026-05-20
        concept: 02-accuracy-data-fidelity
        boundary: Executor
        read_minutes: 2
        hook: "neat idea"
        tags: [data-fidelity, hallucination]
        ---
        **Working toward:** Data Fidelity.

        ## In a nutshell
        Every number is carried untouched.
    """)
    [lesson] = list_lessons(lessons_dir=tmp_path)
    assert lesson.issue is None
    assert lesson.concept == "02-accuracy-data-fidelity"
    assert lesson.read_minutes == 2
    assert "In a nutshell" in lesson.body_markdown


def test_list_lessons_skips_files_without_frontmatter(tmp_path):
    _write(tmp_path, "stray.md", "just text, no frontmatter")
    assert list_lessons(lessons_dir=tmp_path) == []


def test_list_lessons_empty_when_dir_missing(tmp_path):
    assert list_lessons(lessons_dir=tmp_path / "nope") == []


def test_get_lesson_returns_one(tmp_path):
    _write(tmp_path, "one.md", """
        ---
        title: "One"
        date: 2026-06-01
        ---
        hello
    """)
    lesson = get_lesson("one", lessons_dir=tmp_path)
    assert lesson is not None
    assert lesson.title == "One"


def test_get_lesson_returns_none_for_unknown_slug(tmp_path):
    assert get_lesson("nope", lessons_dir=tmp_path) is None


def test_get_lesson_rejects_path_traversal(tmp_path):
    # A slug containing path separators must never escape the lessons dir.
    assert get_lesson("../secret", lessons_dir=tmp_path) is None


def test_get_lesson_rejects_dotdot_slug(tmp_path):
    # The "." allowed in the slug charset must not let a bare ".." token through;
    # any ".." sequence is a path-traversal reference and is rejected.
    assert get_lesson("..", lessons_dir=tmp_path) is None
    assert get_lesson("a..b", lessons_dir=tmp_path) is None
