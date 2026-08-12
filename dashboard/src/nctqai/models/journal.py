"""Pydantic model for a Compass lesson (docs/lessons/*.md).

A lesson's markdown is the single source of truth; this model is the parsed
shape of its YAML frontmatter plus the raw markdown body. Rendering to HTML is
done in the route layer, not here.
"""

from datetime import date as _Date

from pydantic import BaseModel


class Lesson(BaseModel):
    """One Compass lesson, parsed from a docs/lessons/*.md file."""

    model_config = {"extra": "ignore"}

    slug: str                      # filename stem, e.g. "1150-meet-the-bots"
    title: str
    date: _Date | None = None  # `_Date` alias avoids the field name shadowing the imported `date` type
    issue: int | None = None       # absent on non-issue lessons
    pr: int | None = None
    concept: str = ""              # concept slug or "internal"
    boundary: str | None = None
    read_minutes: int | None = None
    hook: str = ""                 # one-line teaser for the feed
    tags: list[str] = []
    body_markdown: str = ""        # raw markdown after the frontmatter
