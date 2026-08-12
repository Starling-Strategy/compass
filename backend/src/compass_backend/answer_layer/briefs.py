"""Build sealed answer briefs from deterministic Compass responses."""

from __future__ import annotations

import re
from collections.abc import Iterable

from compass_backend.artifacts import RESULT_SET_TYPES, ResultSet
from compass_backend.contracts.answer_layer import (
    AdjacentMetric,
    AnswerBrief,
    AnswerFact,
    ImmutableBlock,
)
from compass_backend.contracts.rendering import ResponseManifest

_SOURCE_MARKER_RE = re.compile(r"\[(?:\d+|[A-Za-z][A-Za-z0-9_:-]*)\]")
_NUMERIC_RE = re.compile(r"\$?\d+(?:,\d{3})*(?:\.\d+)?%?")
_MODEL_SAFE_METADATA_KEYS = frozenset(
    {
        "question",
        "artifact_id",
        "row_count",
        "citations",
    }
)
_CANONICAL_CAVEAT_LINE_MARKERS = (
    "data availability:",
    # #942 filtered-lookup district denominator — a load-bearing factual summary
    # the voice pass must preserve (it is prose, so otherwise droppable; cf. C1
    # which sealed its names as a table for the same reason).
    "current evaluation data",
    "does not have current reviewed numeric values",
    "do not have current reviewed numeric values",
    "older-year value",
    "older-year values",
    "not been reviewed for the requested",
    "issue-not-addressed",
    "not applicable",
    "outside the district policy pathfinder",
    "out-of-universe",
    # #1514 canonical coverage narrative: the district-counting availability
    # lead (D9), the stale prior-year sentence (D7), the per-district
    # not-reviewed sentence (rule 2), the out-of-Pathfinder sentence (D6),
    # and the categorical "reviewed value is unavailable for N of the M
    # covered districts" sentence (D12).
    "districts you asked about",
    "district you asked about",
    "nctq last reviewed",
    "hasn't reviewed",
    "last reviewed in an earlier year",
    "not in the district policy pathfinder",
    "reviewed value is unavailable",
    # #1337 / FILT-88: the match-denominator prevalence lead ("Of N covered
    # districts with data, X (Y%) match your criteria." on the count path, and "Of N
    # districts with a current value for <metric>, X (Y%) match your criteria." on
    # the filtered-list path). A load-bearing factual summary the voice pass must
    # not drop; the same marker seals both leads (the count path's was previously
    # unsealed — cf. the threshold-transparency cases 1030/1142).
    "match your criteria",
    "matches your criteria",
    # #1613 U6: the multi-metric-lookup partial-resolution disclosure
    # ("Compass doesn't track <phrase> as a metric, so the table below covers the
    # other metrics you asked about."). A load-bearing honest gap the voice pass
    # must not drop — without it a deferred metric is silently dropped (the
    # over-clarify cluster's silent-drop face). Same seal mechanism as the filter
    # leads above; anchor is case 1193 ("average teacher salary" has no metric).
    "doesn't track",
    # #1651: the vague-quantifier transparency note composer._threshold_hint_line
    # emits ("(interpreting 'real (not token)' as $5,000 or more)"). It names the
    # concrete cutoff Compass applied; without this seal the voice pass deleted it
    # and replaced it with mood words ("a meaningful incentive"), hiding the line a
    # district just below the cutoff fell below. The composer line is the authority
    # for the wording — keep this marker in lockstep with it (the same
    # marker↔text coupling the "match your criteria" lead carries).
    "interpreting '",
)


def build_answer_brief(
    manifest: ResponseManifest,
    *,
    route: str = "execute",
    result_set: ResultSet | None = None,
    caveat_fragments: Iterable[str] = (),
    allowed_nctq_context: Iterable[str] = (),
    suggested_followups: Iterable[str] = (),
    style_notes: Iterable[str] = (),
) -> AnswerBrief:
    """Seal a rendered manifest into the evidence shape sent to the stylist.

    #1759 (strengthen half): when the typed ``result_set`` is threaded in, every
    grounded display value it carries — including ones the deterministic renderer
    placed only in a table or in an excluded-district disclosure, not in the
    narrative body — is sealed as an ``AnswerFact``. The seal allowlist
    (``validation._numeric_findings`` / ``_source_marker_findings``) then covers
    those grounded values, so a richer stylist draft that surfaces a real
    table-only figure (an anchor district's own value, a chart-excluded
    district's prior value) is accepted instead of rejected as a "new numeric
    token" and discarded for the barer body. The guard stays CLOSED to invented
    numbers: only values present on a real result row enter the allowlist.
    """

    metadata = _model_safe_metadata(manifest.metadata)
    question = metadata.get("question")
    artifact_id = metadata.get("artifact_id")
    caveats = _unique(
        [
            *caveat_fragments,
            *canonical_caveat_fragments(manifest.body),
        ]
    )
    raw_adjacents = manifest.metadata.get("adjacent_metrics") or ()
    if isinstance(raw_adjacents, (list, tuple)):
        adjacent_metrics = tuple(
            AdjacentMetric(label=str(entry["label"]))
            for entry in raw_adjacents
            if isinstance(entry, dict) and isinstance(entry.get("label"), str) and entry["label"]
        )
    else:
        adjacent_metrics = ()
    # #1240: the orchestrator stamps this when the user asked for a chart the
    # data could not support (read straight from metadata, mirroring the
    # adjacent_metrics path above). The stylist narrates it naturally.
    chart_unavailable = bool(manifest.metadata.get("chart_unavailable"))
    return AnswerBrief(
        deterministic_body=manifest.body,
        user_question=question if isinstance(question, str) else "",
        route=route,
        result_type=manifest.result_type,
        artifact_id=artifact_id if isinstance(artifact_id, str) else None,
        manifest_metadata=metadata,
        facts=grounded_result_facts(result_set),
        immutable_blocks=tuple(immutable_markdown_blocks(manifest.body)),
        numeric_tokens=numeric_tokens(manifest.body),
        source_markers=source_markers(manifest.body),
        caveat_fragments=caveats,
        allowed_nctq_context=tuple(allowed_nctq_context),
        suggested_followups=tuple(suggested_followups),
        chart_unavailable=chart_unavailable,
        style_notes=tuple(style_notes),
        adjacent_metrics=adjacent_metrics,
        attached_artifacts=_attached_artifacts(manifest.metadata),
    )


def grounded_result_facts(result_set: ResultSet | None) -> tuple[AnswerFact, ...]:
    """Seal every grounded display value on the typed result rows as a fact.

    The deterministic renderer often places a district's value only in the data
    table (and a prior/excluded district's value only in a coverage disclosure),
    never in the narrative body. ``build_answer_brief`` derives the seal's numeric
    and source-marker allowlists from ``manifest.body`` text alone, so those
    table-only values look "new" to the seal and a stylist draft that voices them
    is rejected. Pulling them from the typed rows here widens the allowlist to
    exactly the grounded values — never invented ones, since we read only real
    row fields (``display_value``, ``coverage_prior_display_value``) and run them
    through the same ``numeric_tokens`` extractor the validator uses on the draft.
    """

    if result_set is None or not isinstance(result_set, RESULT_SET_TYPES):
        return ()
    facts: list[AnswerFact] = []
    seen_tokens: set[str] = set()
    for row in _grounded_result_rows(result_set):
        label = getattr(row, "district_name", None)
        if not isinstance(label, str) or not label:
            label = "result"
        markers = tuple(
            f"[{marker}]"
            for marker in getattr(row, "citation_markers", ()) or ()
        )
        for attr in ("display_value", "coverage_prior_display_value"):
            display = getattr(row, attr, None)
            if not isinstance(display, str) or not display:
                continue
            for token in numeric_tokens(display):
                if token in seen_tokens:
                    continue
                seen_tokens.add(token)
                facts.append(
                    AnswerFact(label=label, value=token, source_markers=markers)
                )
    return tuple(facts)


def _grounded_result_rows(result_set: ResultSet) -> list[object]:
    """Flatten a result set's data rows, descending into composite children.

    A ``composite_ranking`` envelope keeps its rows on each child table, so the
    parent's empty ``rows`` would otherwise hide every grounded value.
    """

    rows: list[object] = list(getattr(result_set, "rows", ()) or ())
    for child in getattr(result_set, "children", ()) or ():
        rows.extend(getattr(child, "rows", ()) or ())
    return rows


def _attached_artifacts(metadata: dict[str, object]) -> tuple[str, ...]:
    """Name the artifact surfaces that ship beside the answer text.

    The chart and CSV export travel alongside the markdown body, so they never
    appear inside it — without this signal the stylist can wrongly conclude
    Compass "can't generate a chart". The renderer stamps ``has_chart`` /
    ``has_csv_export`` onto the manifest (writer.render_response); the style
    guide tells the stylist never to deny an artifact listed here.
    """

    artifacts: list[str] = []
    if metadata.get("has_chart"):
        artifacts.append("chart")
    if metadata.get("has_csv_export"):
        artifacts.append("csv_export")
    return tuple(artifacts)


def canonical_caveat_fragments(body: str) -> tuple[str, ...]:
    """Return deterministic caveat lines the answer layer must preserve.

    Tables and Sources blocks are immutable already. This helper seals the
    surrounding coverage/data-availability prose so the gated answer layer can
    improve voice without dropping the user-facing limitation.
    """

    fragments: list[str] = []
    in_sources = False
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        lower = stripped.casefold()
        if lower in {"sources", "source", "sources:", "source:"}:
            in_sources = True
            continue
        if in_sources:
            if stripped.startswith("#") or stripped.startswith("|"):
                in_sources = False
            else:
                continue
        if stripped.startswith("|"):
            continue
        if any(marker in lower for marker in _CANONICAL_CAVEAT_LINE_MARKERS):
            fragments.append(stripped)
    return _unique(fragments)


def immutable_markdown_blocks(body: str) -> tuple[ImmutableBlock, ...]:
    """Extract table, source-list, and section-heading blocks that must survive
    rewriting.

    Tables and source lists are immutable because their cell values and citation
    markers are load-bearing. #1228 adds the H4 section headings (``#### …``) the
    renderer emits for secondary sections (coverage disclosure, methodology,
    "Not included in ranking", …): the frontend folds each H4 section into a
    collapsible, so if the stylist were free to drop or reword the heading the
    fold would silently no-op and the caveat wall would return below the table.
    Sealing the heading here means a draft that drops it fails
    ``_immutable_block_findings`` → the draft is rejected → the deterministic
    body (answer-first, headed) ships instead. This is the #1413 sealed-block
    pattern (matching-district names) applied to section structure.
    """

    lines = body.splitlines()
    blocks: list[ImmutableBlock] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#### "):
            blocks.append(
                ImmutableBlock(
                    kind="section_heading",
                    text=line.strip(),
                    block_id=f"heading-{len(blocks) + 1}",
                )
            )
            index += 1
            continue
        if line.startswith("|"):
            start = index
            while index < len(lines) and lines[index].startswith("|"):
                index += 1
            blocks.append(
                ImmutableBlock(
                    kind="table",
                    text="\n".join(lines[start:index]).strip(),
                    block_id=f"table-{len(blocks) + 1}",
                )
            )
            continue
        if line.strip().casefold() in {"sources", "source", "sources:", "source:"}:
            start = index
            index += 1
            while index < len(lines):
                next_line = lines[index]
                if next_line.startswith("|"):
                    break
                stripped = next_line.strip()
                if stripped.startswith("#"):
                    break
                index += 1
            blocks.append(
                ImmutableBlock(
                    kind="sources",
                    text="\n".join(lines[start:index]).strip(),
                    block_id=f"sources-{len(blocks) + 1}",
                )
            )
            continue
        index += 1
    return tuple(blocks)


def _model_safe_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Keep model-facing brief metadata free of internal diagnostics."""

    return {
        key: value
        for key, value in metadata.items()
        if key in _MODEL_SAFE_METADATA_KEYS
    }


def numeric_tokens(text: str) -> tuple[str, ...]:
    """Return unique numeric tokens, ignoring numbers inside source markers."""

    scrubbed = _SOURCE_MARKER_RE.sub("", text)
    return _unique(_NUMERIC_RE.findall(scrubbed))


def source_markers(text: str) -> tuple[str, ...]:
    """Return unique source/reference markers visible in markdown body text."""

    return _unique(_SOURCE_MARKER_RE.findall(text))


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)
