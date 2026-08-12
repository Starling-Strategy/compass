"""Conversation-with-verdicts route for the Compass Quality dashboard.

GET /compass/quality/conversations/<session_id>
    Renders one conversation turn-by-turn with verdicts attached per turn.
    VerdictList auto-expands on failure. Links back to the Scorecard.
"""
from __future__ import annotations

import logging

from fasthtml.common import A, Div, P, Span
from starlette.requests import Request

from nctqai.components.compass.quality import VerdictList, breadcrumb_strip
from nctqai.layout import Layout
from nctqai.routes.compass._helpers import require_compass
from nctqai.services.compass_quality.loaders import load_conversation_with_verdicts

logger = logging.getLogger(__name__)

_SCORECARD_URL = "/compass/quality/scorecard"


def _conversation_page(request: Request, session_id: str) -> tuple:
    """Render the quality-annotated conversation detail."""
    user = getattr(request.state, "user", None)

    try:
        conv = load_conversation_with_verdicts(session_id)
    except KeyError:
        content = Div(
            P(f"Session not found: {session_id!r}", cls="text-red-600"),
            A("Back to Scorecard", href=_SCORECARD_URL, cls="quality-breadcrumb-link"),
        )
        return Layout(
            "Conversation Not Found",
            "",
            content,
            section="compass",
            sub_nav=_SCORECARD_URL,
            user=user,
            data_cluster="quality",
        )
    except Exception as exc:
        logger.exception("Conversation quality: failed for %s", session_id)
        content = Div(
            P("Could not load conversation.", cls="text-red-600"),
            P(str(exc), cls="text-sm text-gray-500"),
        )
        return Layout(
            "Conversation Error",
            "",
            content,
            section="compass",
            sub_nav=_SCORECARD_URL,
            user=user,
            data_cluster="quality",
        )

    crumbs = breadcrumb_strip([
        ("Compass", "/compass/overview"),
        ("Quality Scorecard", _SCORECARD_URL),
        (f"Session {session_id[:8]}…", None),
    ])

    # Metadata strip
    meta_parts = [
        Span(f"Session: {conv.session_id}", cls="quality-context-value"),
    ]
    if conv.started_at:
        meta_parts.append(Span(f" · Started: {conv.started_at[:10]}", cls="quality-context-label"))
    if conv.trace_id:
        trace_url = f"/compass/v2/traces?trace_id={conv.trace_id}"
        meta_parts.append(
            A(" · View trace", href=trace_url, cls="quality-breadcrumb-link")
        )

    meta_strip = Div(*meta_parts, cls="quality-context-strip", style="margin-bottom:12px;")

    # Turn blocks
    if not conv.turns:
        turns_section = P("No turns recorded for this session.", cls="quality-no-data-pill")
    else:
        turn_blocks = []
        for turn in conv.turns:
            verdict_widget = VerdictList(turn.verdicts, auto_expand_on_failure=True)
            block = Div(
                Div(f"Turn {turn.turn_index}", cls="quality-turn-index"),
                Div(turn.user_text, cls="quality-turn-user"),
                Div(turn.assistant_text, cls="quality-turn-assistant"),
                verdict_widget,
                cls="quality-turn-block",
            )
            turn_blocks.append(block)
        turns_section = Div(*turn_blocks)

    content = Div(crumbs, meta_strip, turns_section)

    page_title = f"Session {session_id[:8]}…"
    if conv.scenario_name:
        page_title = f"{conv.scenario_name} — {session_id[:8]}…"

    return Layout(
        page_title,
        "Turn-by-turn verdicts",
        content,
        section="compass",
        sub_nav=_SCORECARD_URL,
        user=user,
        data_cluster="quality",
    )


def register(rt):
    """Register conversation quality routes with the FastHTML router."""

    @rt("/compass/quality/conversations/{session_id}")
    def get(request: Request, session_id: str):
        _, deny = require_compass(request)
        if deny:
            return deny
        return _conversation_page(request, session_id)
