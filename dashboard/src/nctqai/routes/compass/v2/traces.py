"""/compass/v2/traces — turn/session picker + span-tree explorer.

Queries Logfire for a trace_id's full span tree and renders it nested.
Gracefully degrades when Logfire is unreachable.
"""

from __future__ import annotations

from collections import defaultdict

from fasthtml.common import Div, Form, H2, Input, P
from monsterui.all import Button
from starlette.requests import Request

from nctqai.components.compass.v2.narrative_card import NarrativeCard
from nctqai.components.compass.v2.span_tree import SpanNode
from nctqai.layout import Layout
from nctqai.routes.compass._helpers import require_compass_admin
from nctqai.services.compass_v2.logfire_query import fetch_span_tree


def _build_tree(rows: list[dict]) -> list:
    """Turn flat span rows into a nested SpanNode hierarchy."""
    by_parent: dict[str | None, list[dict]] = defaultdict(list)
    for row in rows:
        by_parent[row.get("parent_span_id")].append(row)

    def node(row: dict, depth: int = 0):
        children = [node(r, depth + 1) for r in by_parent.get(row.get("span_id"), [])]
        attrs = row.get("attributes") or {}
        return SpanNode(
            span_name=row.get("span_name") or "",
            duration_ms=row.get("duration_ms"),
            attributes=attrs,
            children=children,
            depth=depth,
        )

    roots = by_parent.get(None, []) + by_parent.get("", [])
    return [node(r) for r in roots]


def _picker_form(trace_id: str = "") -> Form:
    return Form(
        Input(
            type="text",
            name="trace_id",
            placeholder="trace_id (from a message_feedback row or SSE done event)",
            value=trace_id,
            cls="v2-trace-input",
        ),
        Button("Load tree", cls="v2-trace-submit"),
        method="get",
        action="/compass/v2/traces",
        cls="v2-trace-picker",
    )


def register(rt):
    @rt("/compass/v2/traces")
    async def get_traces(request: Request, trace_id: str = ""):
        user, deny = require_compass_admin(request)
        if deny:
            return deny

        rows = await fetch_span_tree(trace_id) if trace_id else []
        if trace_id and not rows:
            body = P(
                "No spans found for this trace_id. Either the id is wrong or Logfire is unreachable. Check LOGFIRE_READ_TOKEN.",
                cls="v2-trace-empty",
            )
        elif rows:
            body = Div(*_build_tree(rows), cls="v2-trace-tree")
        else:
            body = P(
                "Paste a trace_id above to see the whole turn span tree — agents, tools, rules, stream events.",
                cls="v2-trace-hint",
            )

        content = Div(
            H2("Traces", cls="v2-page-title"),
            NarrativeCard(
                "Start with a trace_id",
                """
Every user turn emits a root compass.turn span with a trace_id. You can get that trace_id from:

• compass.message_feedback.trace_id — for any thumbs-up/down feedback turn
• Any compass.* log line — it's threaded through every SSE event

The span tree shows exactly what happened: which intent was chosen, which tools fired in what order, which rules evaluated with what verdict, how many tokens each step spent.
                """,
            ),
            _picker_form(trace_id),
            body,
            cls="v2-page v2-page-traces uk-container",
        )

        return Layout(
            "Traces",
            "",
            content,
            section="compass",
            sub_nav="/compass/v2/traces",
            user=user,
        )
