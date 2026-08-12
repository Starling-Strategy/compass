"""Journal section — /journal feed + /journal/{slug} lesson pages.

Renders Compass lessons (docs/lessons/*.md) natively in the dashboard chrome.
Markdown bodies are rendered server-side with markdown-it-py (html disabled),
mirroring components/compass/turn_card.py.
"""

from fasthtml.common import A, Div, H3, NotStr, P, Span
from markdown_it import MarkdownIt

from nctqai.layout import Layout
from nctqai.routes._auth import require_section
from nctqai.services.journal import get_lesson, list_lessons

_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


def register_journal_routes(rt):
    """Register the Journal routes on the FastHTML router."""

    @rt("/journal")
    def journal_feed(request):
        user, deny = require_section(request, "journal")
        if deny:
            return deny
        lessons = list_lessons()
        return Layout(
            "Journal",
            f"{len(lessons)} lessons",
            _feed_content(lessons),
            section="journal",
            sub_nav="/journal",
            user=user,
            show_heading=False,
        )

    @rt("/journal/{slug}")
    def journal_lesson(request, slug: str):
        user, deny = require_section(request, "journal")
        if deny:
            return deny
        lesson = get_lesson(slug)
        if lesson is None:
            return Layout(
                "Not Found",
                "",
                P("Lesson not found.", cls="uk-text-muted empty-state"),
                section="journal",
                sub_nav="/journal",
                user=user,
            )
        return Layout(
            lesson.title,
            lesson.hook,
            _lesson_content(lesson),
            section="journal",
            sub_nav="/journal",
            user=user,
            breadcrumb=[("Journal", "/journal"), (lesson.title, None)],
        )


def _meta_line(lesson) -> str:
    """e.g. '2026-06-01 · 2 min read' (date optional, read time optional)."""
    parts = []
    if lesson.date:
        parts.append(lesson.date.isoformat())
    if lesson.read_minutes:
        parts.append(f"{lesson.read_minutes} min read")
    return " · ".join(parts)


def _lesson_card(lesson):
    """One clickable feed card."""
    return A(
        Div(
            Span(_meta_line(lesson), cls="review-card-label"),
            H3(lesson.title),
            P(lesson.hook, cls="uk-text-muted"),
            cls="overview-panel",
        ),
        href=f"/journal/{lesson.slug}",
        cls="uk-link-reset",
    )


def _feed_content(lessons):
    """The feed body: a card grid, or an empty state."""
    if not lessons:
        return P("No lessons yet.", cls="uk-text-muted empty-state")
    return Div(*[_lesson_card(lesson) for lesson in lessons], cls="overview-grid-2")


def _lesson_content(lesson):
    """One lesson page body: back link + meta + rendered markdown."""
    header = Div(
        A("← Back to Journal", href="/journal", cls="uk-link-text text-sm"),
        Span(_meta_line(lesson), cls="review-card-label"),
        cls="uk-margin-bottom",
    )
    body = Div(NotStr(_MARKDOWN.render(lesson.body_markdown)), cls="lesson-body")
    return Div(header, body)
