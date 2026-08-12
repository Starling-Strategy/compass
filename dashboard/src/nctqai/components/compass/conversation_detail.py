"""Conversation detail rendering — header card + turn cards.

Renders the right-pane detail of /compass/conversations. Reads directly from
the canonical compass.* Postgres schema via ``services.compass_conversations``.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from datetime import datetime
from urllib.parse import quote

from fasthtml.common import (
    A,
    Button,
    Div,
    Form,
    H2,
    NotStr,
    Option,
    P,
    Script,
    Select,
    Span,
    Textarea,
)

# Outline link icon (real SVG, no emoji) for the permalink chip.
_PERMALINK_ICON = (
    "<svg width='15' height='15' viewBox='0 0 24 24' fill='none' stroke='currentColor' "
    "stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>"
    "<path d='M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71'/>"
    "<path d='M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71'/></svg>"
)

from compass_backend.quality.scorecard_models import ConversationTurn, Verdict
from nctqai.components.compass.flag_dimensions import FLAG_DIMENSIONS
from nctqai.components.compass.timestamps import format_eastern_timestamp
from nctqai.components.compass.turn_card import TurnCard
from nctqai.db import COMPASS_SCHEMA, run_sql
from nctqai.models.compass import ConversationSummary
from nctqai.services.compass_conversations import (
    get_conversation,
    get_session_feedback,
)
from nctqai.services.compass_quality.loaders import load_conversation_with_verdicts
from nctqai.services.compass_snapshot_memory import SNAPSHOT_KEY

logger = logging.getLogger(__name__)


def _as_mapping(value: object) -> dict:
    """Tolerate jsonb handed back as a dict OR a JSON string (run_sql does both).

    Mirrors the dict-or-str tolerance ``compass_snapshot_memory`` already uses.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _snapshots_by_msg_id(session_id: str) -> dict[int, dict]:
    """Return ``{assistant_message_id: result_dict}`` for the session.

    The sibling-owned ``get_conversation`` loader only selects the text columns,
    so the saved fresh-turn snapshot (rows / citations / csv_export) isn't on the
    ChatMessage rows. We read it here, keyed by the assistant message id — a
    direct, exact key, so no content-alignment fragility (unlike the verdict
    join). Read-only and resilient (DASH-R7): any failure degrades to ``{}`` so
    the detail still renders prose, never a 500.
    """
    try:
        rows = run_sql(
            f"""
            SELECT id,
                   message_data->'{SNAPSHOT_KEY}'->'result' AS result
              FROM {COMPASS_SCHEMA}.chat_messages
             WHERE session_id = %s
               AND role = 'assistant'
               AND message_data ? '{SNAPSHOT_KEY}'
            """,
            (session_id,),
        )
    except Exception:
        logger.exception("conversation detail: snapshot load failed for %s", session_id)
        return {}

    result: dict[int, dict] = {}
    for row in rows:
        msg_id = row.get("id")
        if msg_id is None:
            continue
        result[msg_id] = _as_mapping(row.get("result"))
    return result


def _csv_cell(value: object) -> str:
    """Stringify a saved csv_export cell verbatim (lists join with commas)."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _csv_filename(user_message: str | None, turn_number: int) -> str:
    """Human-readable download filename derived from the turn's question."""
    slug = re.sub(r"[^a-z0-9]+", "-", (user_message or "").lower()).strip("-")
    slug = "-".join(slug.split("-")[:6]) or "compass-data"
    return f"{slug}-turn{turn_number}.csv"


def csv_download_link(csv_export: object, *, filename: str) -> A | None:
    """Build a 'Download CSV' link from the saved verbatim ``csv_export``.

    ``csv_export`` is the snapshot's ``{columns, rows}`` artifact. The bytes are
    the saved columns/rows VERBATIM — no recomputation — so the downloaded file
    matches what the user exported. We embed the CSV as a ``data:`` URI so no new
    server route is needed (routes are sibling-owned in ``conversations.py``).
    Returns None when there is nothing to download.
    """
    export = _as_mapping(csv_export)
    columns = export.get("columns")
    rows = export.get("rows")
    if not isinstance(columns, list) or not columns:
        return None
    if not isinstance(rows, list) or not rows:
        return None

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([str(col) for col in columns])
    for row in rows:
        if not isinstance(row, dict):
            continue
        writer.writerow([_csv_cell(row.get(col)) for col in columns])

    href = "data:text/csv;charset=utf-8," + quote(buffer.getvalue())
    return A(
        "Download CSV",
        href=href,
        download=filename,
        cls="compass-csv-download",
    )


def _verdicts_for_rendered_turns(
    turns: list[tuple], backend_turns: list[ConversationTurn]
) -> list[list[Verdict] | None]:
    """Attach backend per-turn verdicts to the detail view's rendered cards.

    The detail pane (``group_into_turns`` over ``get_conversation``) and the
    backend (``load_conversation_turns``) filter messages DIFFERENTLY: the
    backend drops rows with ``length(content) <= 5`` *before* pairing, so a
    short message ("ok", "why?") doesn't just disappear as a turn — it shifts
    which user pairs with which assistant. A positional counter would silently
    misattach verdicts in that case.

    So we align by CONTENT, not by index: walk the rendered ``(user,
    assistant)`` cards with a pointer into the backend's ordered turns; both
    lists are ordered by ``(timestamp, id)``. When a rendered pair's text
    matches the next unconsumed backend turn, attach its verdicts and advance
    the pointer. A card that matches no backend turn (e.g. an orphan/short turn
    the backend dropped) gets ``None`` and renders no verdict block. This is
    correct regardless of how the two filters diverge.

    Returns one entry per rendered turn: the matched verdict list, or None.
    """
    result: list[list[Verdict] | None] = []
    ptr = 0
    for user_msg, assistant_msg in turns:
        matched: list[Verdict] | None = None
        if (
            ptr < len(backend_turns)
            and user_msg is not None
            and assistant_msg is not None
            and (user_msg.content or "") == backend_turns[ptr].user_text
            and (assistant_msg.content or "") == backend_turns[ptr].assistant_text
        ):
            matched = backend_turns[ptr].verdicts
            ptr += 1
        result.append(matched)
    return result


def verdict_status(convo: ConversationSummary) -> str | None:
    """Map ConversationSummary.last_verdict_approved to a verdict status string."""
    if convo.last_verdict_approved is True:
        return "approved"
    if convo.last_verdict_approved is False:
        return "revision_needed"
    return None


def group_into_turns(messages: list) -> list[tuple]:
    """Group ordered messages into (user_msg, assistant_msg) turn pairs."""
    turns = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.role == "user":
            user_msg = msg
            assistant_msg = None
            if i + 1 < len(messages) and messages[i + 1].role == "assistant":
                assistant_msg = messages[i + 1]
                i += 2
            else:
                i += 1
            turns.append((user_msg, assistant_msg))
        else:
            turns.append((None, msg))
            i += 1
    return turns


def permalink_banner() -> Div:
    """URL chip + Copy-link button shown above the detail header card.

    The URL itself is filled in by inline JS at render time from
    ``window.location.href`` so the banner stays correct on HTMX swaps,
    deep links, and slugged URLs (when those land in a later tier).
    """
    return Div(
        Span(NotStr(_PERMALINK_ICON), cls="compass-permalink-icon"),
        Span(cls="compass-permalink-url", id="compass-permalink-url"),
        Button(
            "Copy link",
            type="button",
            cls="compass-permalink-copy",
            onclick=(
                "navigator.clipboard.writeText(window.location.href)"
                ".then(() => { this.textContent = 'Copied!'; "
                "setTimeout(() => this.textContent = 'Copy link', 1500); })"
            ),
            title="Copy a shareable link to this conversation",
        ),
        # Tiny JS sets the URL chip to the current location on mount. The
        # detail pane is HTMX-swapped, so this runs once per swap because
        # the script tag itself re-enters the DOM with the swap.
        Script(
            "(function(){var el=document.getElementById('compass-permalink-url');"
            "if(el){el.textContent=window.location.href;}})();"
        ),
        cls="compass-permalink-banner",
    )


def detail_header(
    convo: ConversationSummary,
    *,
    session_id: str | None = None,
    created_at: datetime | None = None,
    turn_count: int | None = None,
) -> Div:
    """Detail-pane header rendered as a single white card.

    Replaces the old position:sticky strip — same content, but now grouped
    inside one card (matches v3 detailHead) so the question, meta, and
    badges read as one unit instead of fighting Turn 1's card for the
    reader's eye.
    """
    verdict_cls_map = {
        True: "compass-badge-approved",
        False: "compass-badge-rejected",
        None: "compass-badge-none",
    }
    verdict_label_map = {True: "pass", False: "fail", None: ""}
    verdict_badge = None
    if convo.last_verdict_approved is not None:
        verdict_badge = Span(
            verdict_label_map[convo.last_verdict_approved],
            cls=f"compass-badge {verdict_cls_map[convo.last_verdict_approved]}",
        )

    thumbs = []
    if convo.thumbs_up_count:
        thumbs.append(
            Span(f"{convo.thumbs_up_count} helpful", cls="compass-badge compass-badge-thumbs-up")
        )
    if convo.thumbs_down_count:
        thumbs.append(
            Span(
                f"{convo.thumbs_down_count} flagged", cls="compass-badge compass-badge-thumbs-down"
            )
        )
    badges_row = [b for b in [verdict_badge, *thumbs] if b is not None]

    district_str = ""
    if convo.district_count > 1:
        district_str = f"{convo.district_count} districts in context"
    elif convo.district_name:
        district_str = f"District context: {convo.district_name}"
        if convo.state:
            district_str += f", {convo.state}"

    question = convo.first_user_message or "(no question text)"

    meta_parts: list = []
    if district_str:
        meta_parts.append(
            Span(
                district_str,
                cls="compass-detail-meta-item",
                title="District saved in Compass's conversation context. This is not the user's identity or location.",
            )
        )
    if turn_count is not None:
        meta_parts.append(
            Span(
                f"{turn_count} turn{'s' if turn_count != 1 else ''}",
                cls="compass-detail-meta-item",
            )
        )
    if created_at:
        meta_parts.append(
            Span(format_eastern_timestamp(created_at), cls="compass-detail-meta-item")
        )
    if session_id:
        meta_parts.append(
            Span(session_id[:12], cls="compass-detail-meta-id", title=session_id)
        )

    children: list = [H2(question, cls="compass-detail-question")]
    if meta_parts:
        children.append(Div(*meta_parts, cls="compass-detail-meta-row"))
    if badges_row:
        children.append(Div(*badges_row, cls="compass-detail-badges"))

    return Div(*children, cls="compass-detail-header")


# Back-compat alias — the old name is referenced by tests and callers that
# pre-date the white-card refactor.
sticky_header = detail_header


def empty_state() -> Div:
    """Rendered in the detail pane when nothing is selected."""
    return Div(
        Div(
            Div("Compass", cls="compass-empty-brand"),
            P(
                "Select a conversation from the list to review its quality.",
                cls="compass-empty-desc",
            ),
            cls="compass-empty-content",
        ),
        cls="compass-empty",
    )


def flag_control(session_id: str) -> Div:
    """'Flag for review' control mounted at the bottom of the detail pane.

    Writes a new ``compass.case_reports`` row (DASH-R6) by POSTing to the
    nctqai flag route, which proxies to the backend ``submit_report`` endpoint
    (nctqai never writes ``compass.*`` directly). A whole-conversation flag has
    no natural verdict, so we reuse the backend's required ``outcome`` field as
    the reviewer's judgment; the default ``fail`` lands the flag in the Flagged
    Issues open queue. The form swaps only the small ``#compass-flag-status``
    region (an ``aria-live`` slot) on submit, so the verdict-bearing detail body
    above it is never re-rendered — this is the shared seam with the inline
    verdicts that already live in ``build_detail``.

    The Quality cluster stays on brand blue (the deliberately-sober palette),
    so this control reuses the ``quality-reports-*`` banner/button tokens rather
    than the Compass-body teal.
    """
    # A review flag is inherently fail/partial — there's no reason to flag a
    # conversation as "pass" (it would be excluded from the default Flagged
    # Issues queue anyway), so the control offers only the two failing verdicts.
    outcome_opts = [
        Option("Needs a fix (fail)", value="fail", selected=True),
        Option("Partly wrong (partial)", value="partial"),
    ]
    return Div(
        Div("Flag for weekly review", cls="compass-flag-heading"),
        Form(
            Select(
                *outcome_opts,
                name="outcome",
                cls="compass-flag-outcome",
                aria_label="Flag outcome",
            ),
            Select(
                *[
                    Option(label, value=value, selected=value == "not_sure")
                    for value, label in FLAG_DIMENSIONS
                ],
                name="dimension",
                cls="compass-flag-outcome",
                aria_label="Issue dimension",
            ),
            Textarea(
                name="comments",
                placeholder="What should the reviewer look at? (optional)",
                rows="2",
                cls="compass-flag-comments",
                aria_label="Flag comment",
            ),
            Button("Flag for review", type="submit", cls="compass-flag-submit"),
            hx_post=f"/compass/conversations/{session_id}/flag",
            hx_target="#compass-flag-status",
            hx_swap="innerHTML",
            cls="compass-flag-form",
        ),
        # aria-live slot the POST swaps the ok/error banner into. htmx does NOT
        # set aria-busy and CSS can't toggle an attribute, so the panel (the
        # common ancestor of the form trigger and this region) carries hx-on
        # handlers that flip aria-busy on #compass-flag-status around the swap —
        # the same bubbling pattern the conversations list uses. The
        # [aria-busy="true"] fade hook is U1-owned in theme.py.
        Div(
            id="compass-flag-status",
            cls="compass-flag-status",
            aria_live="polite",
            aria_busy="false",
        ),
        cls="compass-flag-panel",
        hx_on__before_request=(
            "document.getElementById('compass-flag-status')"
            "?.setAttribute('aria-busy','true')"
        ),
        hx_on__after_request=(
            "document.getElementById('compass-flag-status')"
            "?.setAttribute('aria-busy','false')"
        ),
    )


def build_detail(session_id: str, convo: ConversationSummary | None = None) -> Div:
    """Build the full detail panel: sticky header + turn cards.

    Synchronous — reads Postgres directly. The route layer calls this from a
    starlette async handler; the queries are short-lived and pooled, so we
    don't bother routing them through asyncio.

    Args:
        session_id: Session to render.
        convo: Summary row for the sticky header. If None (e.g. deep-link to
            a session not in the currently-loaded list), we still render the
            messages and verdicts; the header just gets less context.
    """
    conversation = get_conversation(session_id)
    if not conversation:
        return Div(
            P("Conversation not found.", cls="compass-empty-desc"),
            cls="compass-empty",
        )

    feedback_rows = get_session_feedback(session_id)

    feedback_by_msg_id: dict[int, object] = {
        f.message_id: f for f in feedback_rows if f.message_id is not None
    }

    turns = group_into_turns(conversation.messages)

    # Real per-turn L1 verdicts from the saved compass.verdicts ledger — the
    # same source /compass/quality/conversations/{id} reads (DASH-R5: unify the
    # two detail views). Read-only and resilient (DASH-R7): a missing session or
    # a backend/pool failure degrades to "no verdicts", never a 500.
    verdicts_by_turn: list[list[Verdict] | None] = [None] * len(turns)
    try:
        backend_conv = load_conversation_with_verdicts(session_id)
        verdicts_by_turn = _verdicts_for_rendered_turns(turns, backend_conv.turns)
    except KeyError:
        pass  # session not in the verdict view → render turns without verdicts
    except Exception:
        logger.exception(
            "conversation detail: verdict load failed for %s", session_id
        )

    # Saved per-turn snapshots (rows / citations / csv_export), keyed by the
    # assistant message id. The text-only loader doesn't carry these, so we read
    # them here; failures degrade to no table/citations/download, never a 500.
    snapshots = _snapshots_by_msg_id(session_id)

    turn_cards = []
    turn_pip_states: list[str] = []
    for i, (user_msg, assistant_msg) in enumerate(turns):
        fb = None
        snapshot: dict = {}
        if assistant_msg is not None and assistant_msg.id is not None:
            fb = feedback_by_msg_id.get(assistant_msg.id)
            snapshot = snapshots.get(assistant_msg.id, {})

        snapshot_rows = snapshot.get("rows")
        citations = snapshot.get("citations")
        chart = snapshot.get("chart")
        csv_link = csv_download_link(
            snapshot.get("csv_export"),
            filename=_csv_filename(user_msg.content if user_msg else "", i + 1),
        )

        l1_verdicts = verdicts_by_turn[i]

        # Pip state: a turn with any failing L1 verdict colors its pip "fail";
        # otherwise fall back to the user-feedback signal (the only signal we
        # had before verdicts were unified inline).
        if l1_verdicts and any(v.outcome == "fail" for v in l1_verdicts):
            turn_pip_states.append("fail")
        elif fb is not None and fb.rating is not None and fb.rating < 0:
            turn_pip_states.append("fail")
        elif fb is not None and fb.rating is not None and fb.rating > 0:
            turn_pip_states.append("pass")
        else:
            turn_pip_states.append("neutral")

        turn_cards.append(
            TurnCard(
                turn_number=i + 1,
                user_message=user_msg.content if user_msg else "(no user message)",
                assistant_response=assistant_msg.content if assistant_msg else "(no response)",
                timestamp=user_msg.timestamp if user_msg else None,
                feedback_rating=fb.rating if fb else None,
                feedback_tags=fb.feedback_tags if fb else None,
                feedback_text=fb.feedback_text if fb else None,
                l1_verdicts=l1_verdicts,
                snapshot_rows=snapshot_rows if isinstance(snapshot_rows, list) else None,
                citations=citations if isinstance(citations, list) else None,
                csv_download=csv_link,
                chart=chart if isinstance(chart, dict) else None,
            )
        )

    header = (
        detail_header(
            convo,
            session_id=session_id,
            created_at=conversation.created_at,
            turn_count=len(turns),
        )
        if convo
        else None
    )
    nav = turn_navigator(turn_pip_states) if len(turn_pip_states) > 1 else None
    # The flag control is appended as ONE child, after the turn cards. It swaps
    # only its own #compass-flag-status slot, so it never re-renders the
    # verdict-bearing turn cards above it (the shared seam with the inline
    # verdicts already in this detail).
    return Div(
        permalink_banner(), header, nav, *turn_cards, flag_control(session_id)
    )


def turn_navigator(turn_states: list[str]) -> Div:
    """Pip strip above the turn cards — one anchor per turn.

    ``turn_states`` is an ordered list of "pass" / "fail" / "neutral", one
    per turn. Pips with state="fail" render with a red tint so the eye
    lands on the failing turn first.
    """
    pips = []
    for i, state in enumerate(turn_states, start=1):
        cls = f"compass-turn-pip compass-turn-pip-{state}"
        pips.append(
            A(f"T{i}", href=f"#turn-{i}", cls=cls)
        )
    return Div(*pips, cls="compass-turn-nav")
