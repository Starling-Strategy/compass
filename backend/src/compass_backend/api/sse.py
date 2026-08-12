"""SSE serialization for fresh chat responses."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from compass_backend.artifacts import CitationRef, ResultSet
from compass_backend.observability import compass_span, set_span_attributes
from compass_backend.contracts import ChatResponse


_BODY_CITATION_MARKER_RE = re.compile(r"\[(\d+)\]")


def chat_response_sse_payloads(response: ChatResponse) -> Iterable[dict[str, str]]:
    """Return SSE event payloads for one chat response."""

    manifest_status = (
        response.manifest.status if response.manifest is not None else None
    )
    if response.result is not None:
        result_type: str | None = response.result.result_type
    elif response.manifest is not None:
        result_type = response.manifest.result_type
    else:
        result_type = None

    with compass_span(
        "compass.sse.emit",
        session_id=response.session.session_id,
        trace_id=response.trace_id,
        snapshot_id=response.snapshot_id,
        manifest_status=manifest_status,
        result_type=result_type,
    ) as span:
        event_types: list[str] = []

        yield _event(
            "trace",
            {
                "trace_id": response.trace_id,
                "logfire_url": response.logfire_url,
                "session_id": response.session.session_id,
                "snapshot_id": response.snapshot_id,
            },
        )
        event_types.append("trace")

        yield _event("status", {"message": "Compass response ready."})
        event_types.append("status")

        yield _event("text", {"content": response.message})
        event_types.append("text")

        # #1348: structured, clickable clarification options. When a clarify
        # turn carries grounded `candidate_options` (born from real catalog
        # candidates upstream), surface them as a dedicated `options` event so
        # the frontend can render clickable choices alongside the prose. Empty
        # options emit no event, so the prose-only clarify path is unchanged.
        # Ordering is text -> options -> done.
        had_options = False
        clarification = (
            response.turn.clarification if response.turn.route == "clarify" else None
        )
        if clarification is not None and clarification.candidate_options:
            yield _event(
                "options",
                {
                    "mode": "clarify",
                    "missing_fields": list(clarification.missing_fields),
                    "options": [
                        option.model_dump(mode="json")
                        for option in clarification.candidate_options
                    ],
                },
            )
            event_types.append("options")
            had_options = True

        artifacts_are_valid = (
            response.manifest is None or response.manifest.status != "validation_failed"
        )
        had_data = False
        had_citations = False
        had_chart = False

        if response.result is not None and artifacts_are_valid:
            result_payload = response.result.model_dump(mode="json")
            rows_payload = result_payload.get("rows", [])
            data_payload = {
                "result": result_payload,
                "rows": rows_payload,
                "results": rows_payload,
                "result_type": result_payload.get("result_type"),
                "source_notes": result_payload.get("source_notes", []),
                "csv_export": result_payload.get("csv_export"),
            }
            if response.validation is not None:
                data_payload["validation"] = response.validation.model_dump(mode="json")
            if response.manifest is not None:
                data_payload["manifest"] = response.manifest.model_dump(mode="json")
            yield _event("data", data_payload)
            event_types.append("data")
            had_data = True
            result_citations = _result_citations(response.result, response.message)
            if result_citations:
                yield _event(
                    "citations",
                    {
                        "citations": [
                            {"id": citation.marker, **citation.model_dump(mode="json")}
                            for citation in result_citations
                        ]
                    },
                )
                event_types.append("citations")
                had_citations = True
            if result_payload.get("chart") is not None:
                yield _event("chart", result_payload["chart"])
                event_types.append("chart")
                had_chart = True
        elif response.result is None and response.manifest is not None:
            data_payload = {
                "manifest": response.manifest.model_dump(mode="json"),
                "result_type": response.manifest.result_type,
            }
            if response.validation is not None:
                data_payload["validation"] = response.validation.model_dump(mode="json")
            yield _event("data", data_payload)
            event_types.append("data")
            had_data = True
            manifest_citations = _manifest_citations(response)
            if response.manifest.status == "rendered" and manifest_citations:
                yield _event("citations", {"citations": manifest_citations})
                event_types.append("citations")
                had_citations = True

        # #1555: "want a chart?" follow-up suggestion. The orchestrator stamps
        # the prompts onto the manifest when the data was chartable but the user
        # didn't ask (resolve_chart_visibility -> manifest.metadata). Emit them
        # as a dedicated `followups` event so the frontend can render clickable
        # chips. Empty/absent prompts emit no event. Ordering: data/chart ->
        # followups -> done.
        had_followups = False
        followup_prompts = _followup_prompts(response)
        if followup_prompts:
            yield _event("followups", {"prompts": followup_prompts})
            event_types.append("followups")
            had_followups = True

        # Attach aggregates before the final yield so the span carries them
        # even if the consumer reads `done` and immediately closes.
        set_span_attributes(
            span,
            event_count=len(event_types) + 1,
            event_types=event_types + ["done"],
            had_data=had_data,
            had_chart=had_chart,
            had_citations=had_citations,
            had_options=had_options,
            had_followups=had_followups,
        )

        yield _event(
            "done",
            {
                "session_id": response.session.session_id,
                "snapshot_id": response.snapshot_id,
                "trace_id": response.trace_id,
                "logfire_url": response.logfire_url,
                "message_ids": response.message_ids,
            },
        )


def _event(event: str, data: dict[str, Any]) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data)}


def _followup_prompts(response: ChatResponse) -> list[str]:
    """Return non-empty suggested follow-up prompt strings, if any.

    The orchestrator stamps these onto ``manifest.metadata`` when the result
    was chartable but the user didn't request a chart (#1555). Reads defensively
    — only string prompts survive — so a malformed metadata value emits nothing
    rather than a broken event.
    """

    if response.manifest is None:
        return []
    raw = response.manifest.metadata.get("suggested_followups")
    if not isinstance(raw, (list, tuple)):
        return []
    return [prompt for prompt in raw if isinstance(prompt, str) and prompt]


def _manifest_citations(response: ChatResponse) -> list[dict[str, Any]]:
    if response.manifest is None:
        return []
    citations = response.manifest.metadata.get("citations")
    if not isinstance(citations, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, citation in enumerate(citations, start=1):
        if not isinstance(citation, dict):
            continue
        payload = dict(citation)
        payload.setdefault("id", index)
        payload.setdefault("marker", f"[{payload['id']}]")
        normalized.append(payload)
    return normalized


def _result_citations(result: ResultSet | None, body: str) -> list[CitationRef]:
    """Return the current result's answer-level citations.

    The top-level ``result.citations`` list is the normal authority. Composite
    ranking children can also carry citations; descend into them so an
    incomplete envelope cannot make the SSE Sources panel omit a marker that
    appears in this turn's rendered body.
    """

    if result is None:
        return []
    by_marker: dict[int, CitationRef] = {}
    for citation in getattr(result, "citations", []) or []:
        by_marker.setdefault(citation.marker, citation)
    for child in getattr(result, "children", []) or []:
        for citation in getattr(child, "citations", []) or []:
            by_marker.setdefault(citation.marker, citation)
    body_markers = _body_citation_markers(body)
    ordered_markers = [
        *[marker for marker in body_markers if marker in by_marker],
        *[marker for marker in sorted(by_marker) if marker not in body_markers],
    ]
    return [by_marker[marker] for marker in ordered_markers]


def _body_citation_markers(body: str) -> list[int]:
    seen: set[int] = set()
    markers: list[int] = []
    for match in _BODY_CITATION_MARKER_RE.finditer(body):
        marker = int(match.group(1))
        if marker in seen:
            continue
        seen.add(marker)
        markers.append(marker)
    return markers
