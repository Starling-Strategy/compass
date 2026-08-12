"""VerdictList — collapsible verdict display for one conversation turn.

Collapsed by default; auto-expands when any verdict is a failure.
Renders each verdict as a row: criterion_id, judge_source badge, outcome icon, reason.

"Not applicable" verdicts (outcome="error" with a reason prefixed
"not applicable:") are NOT genuine errors — a validator fired on a turn it had
no constraint to evaluate. They are rendered MUTED (the `.quality-verdict-na`
class) and collapsed behind a disclosure so a healthy answer doesn't read as a
wall of red ERRORs. See `is_not_applicable` in
`nctqai.services.compass_quality.models` for the classification and the backend
authority it cites.
"""
from __future__ import annotations

from fasthtml.common import Details, P, Span, Summary, Td, Tr, Table, Thead, Tbody, Th

from compass_backend.quality.scorecard_models import Verdict
from nctqai.services.compass_quality.models import is_not_applicable


_OUTCOME_ICON = {
    "pass": "✓",
    "fail": "✗",
    "error": "⊘",
}

_OUTCOME_CLS = {
    "pass": "quality-verdict-pass",
    "fail": "quality-verdict-fail",
    "error": "quality-verdict-error",
}

# Muted (NOT red) style for not-applicable rows — defined by the THEME agent in
# theme.py (cross-agent contract C-3).
_NA_CLS = "quality-verdict-na"

_SOURCE_LABELS = {
    "deterministic": "deterministic",
    "judge_prompt": "LLM judge",
    "span_assertion": "span",
    "inline_deterministic": "inline",
}


def _verdict_row(v: Verdict, *, na: bool) -> Tr:
    """Render one verdict as a table row.

    na=True renders the muted not-applicable style and an "N/A" label instead of
    the red "ERROR" the raw outcome="error" would otherwise produce.
    """
    if na:
        outcome_cls = _NA_CLS
        icon = "—"
        label = "N/A"
    else:
        outcome_cls = _OUTCOME_CLS.get(v.outcome, "quality-verdict-error")
        icon = _OUTCOME_ICON.get(v.outcome, "?")
        label = v.outcome.upper()

    source_label = _SOURCE_LABELS.get(v.judge_source, v.judge_source)

    return Tr(
        Td(
            Span(icon, cls=f"quality-verdict-icon {outcome_cls}"),
            label,
            cls="quality-verdict-outcome",
        ),
        Td(v.criterion_id, cls="quality-verdict-criterion"),
        Td(
            Span(source_label, cls="quality-verdict-source-badge"),
            cls="quality-verdict-source",
        ),
        Td(v.reason or "—", cls="quality-verdict-reason"),
        cls=f"quality-verdict-row {outcome_cls}",
    )


def _verdict_table(verdicts: list[Verdict], *, na: bool) -> Table:
    rows = [_verdict_row(v, na=na) for v in verdicts]
    return Table(
        Thead(
            Tr(
                Th("Outcome", cls="quality-verdict-th"),
                Th("Criterion", cls="quality-verdict-th"),
                Th("Judge", cls="quality-verdict-th"),
                Th("Reason", cls="quality-verdict-th"),
            )
        ),
        Tbody(*rows),
        cls="quality-verdict-table",
    )


def VerdictList(verdicts: list[Verdict], *, auto_expand_on_failure: bool = True) -> Details:
    """Render verdicts for one turn as a collapsible <details> block.

    Args:
        verdicts: Verdicts to render. Empty list → shows "No verdicts".
        auto_expand_on_failure: When True, auto-expands if any verdict fails
                                (i.e., outcome == "fail"). Genuine errors and
                                not-applicable rows do NOT trigger auto-expand.
    """
    has_failure = any(v.outcome == "fail" for v in verdicts)
    should_open = auto_expand_on_failure and has_failure

    if not verdicts:
        return Details(
            Summary("Verdicts (0)"),
            P("No verdicts recorded for this turn.", cls="quality-verdict-empty"),
            cls="quality-verdict-list",
        )

    # Partition: not-applicable is its own bucket (muted, collapsed). The
    # remainder split into fails (lead), genuine errors, and passes. A genuine
    # error is outcome="error" WITHOUT the "not applicable:" reason prefix.
    na_verdicts = [v for v in verdicts if is_not_applicable(v)]
    fails = [v for v in verdicts if v.outcome == "fail"]
    genuine_errors = [
        v for v in verdicts if v.outcome == "error" and not is_not_applicable(v)
    ]
    passes = [v for v in verdicts if v.outcome == "pass"]

    # Header shows the full shape, reconciling to the total: fails + passes +
    # not-applicable (+ genuine errors only when any exist).
    parts = [
        f"{len(fails)} failed",
        f"{len(passes)} passed",
        f"{len(na_verdicts)} not applicable",
    ]
    if genuine_errors:
        # Insert the genuine-error count after fails so the most severe buckets
        # lead. Only surfaced when nonzero — a healthy turn never shows it.
        parts.insert(1, f"{len(genuine_errors)} errored")
    summary_text = f"Verdicts ({len(verdicts)}) — " + " · ".join(parts)

    # Lead with fails, then genuine errors, then passes — the rows a reviewer
    # cares about first.
    visible = fails + genuine_errors + passes
    body: list = []
    if visible:
        body.append(_verdict_table(visible, na=False))

    # Not-applicable rows are collapsed by default behind their own disclosure
    # so they don't drown the signal. Nested INSIDE the outer <details>, so the
    # outer details stays the first <details> tag (the conversation-page collapse
    # test inspects the first tag).
    if na_verdicts:
        body.append(
            Details(
                Summary(f"Show {len(na_verdicts)} not-applicable"),
                _verdict_table(na_verdicts, na=True),
                cls="quality-verdict-na-disclosure",
            )
        )

    attrs = {"cls": "quality-verdict-list"}
    if should_open:
        attrs["open"] = True

    return Details(
        Summary(summary_text),
        *body,
        **attrs,
    )
