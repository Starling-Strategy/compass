"""Read Compass lessons from docs/lessons/ for the Journal feed.

The lesson markdown is the single source of truth — this module only reads and
parses it (no database, no generated manifest). Frontmatter parsing mirrors
scripts/build_lessons.py:split_frontmatter so the two stay consistent.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

from nctqai.config import Config
from nctqai.models.journal import Lesson

_EXCLUDE = {"README.md", "_TEMPLATE.md"}
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _lessons_dir() -> Path:
    """Resolve the lessons directory: env override, else repo-root/docs/lessons."""
    override = Config().lessons_dir
    if override:
        return Path(override)
    # services/journal.py -> services -> nctqai -> src -> <repo root>
    return Path(__file__).resolve().parents[3] / "docs" / "lessons"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from the markdown body (mirrors build_lessons.py)."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = yaml.safe_load(text[3:end]) or {}
            return fm, text[end + 4 :].lstrip("\n")
    return {}, text


def _parse(path: Path) -> Lesson | None:
    """Parse one lesson file. Returns None if it has no usable frontmatter."""
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    if not fm or "title" not in fm:
        return None
    return Lesson(
        slug=path.stem,
        title=fm["title"],
        date=fm.get("date"),
        issue=fm.get("issue"),
        pr=fm.get("pr"),
        concept=fm.get("concept", ""),
        boundary=fm.get("boundary"),
        read_minutes=fm.get("read_minutes"),
        hook=fm.get("hook", ""),
        tags=fm.get("tags") or [],
        body_markdown=body,
    )


def list_lessons(lessons_dir: Path | None = None) -> list[Lesson]:
    """All lessons, newest first. `lessons_dir` overrides the default (for tests)."""
    directory = lessons_dir or _lessons_dir()
    if not directory.is_dir():
        return []
    lessons: list[Lesson] = []
    for path in directory.glob("*.md"):
        if path.name in _EXCLUDE:
            continue
        lesson = _parse(path)
        if lesson is not None:
            lessons.append(lesson)
    lessons.sort(key=lambda lesson: (lesson.date or date.min), reverse=True)
    return lessons


def get_lesson(slug: str, lessons_dir: Path | None = None) -> Lesson | None:
    """One lesson by filename stem, or None. Rejects path-traversal slugs."""
    if ".." in slug or not _SLUG_RE.match(slug):
        return None
    directory = lessons_dir or _lessons_dir()
    path = directory / f"{slug}.md"
    if not path.is_file():
        return None
    return _parse(path)
