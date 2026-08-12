"""Deterministic grounding of a planner-emitted clarify's options (#1348, Option B).

Covers two dimensions — ``district`` and ``metric`` — with the same shape: read
the phrase(s) the planner is asking about from its *typed* clarify output,
re-resolve them through the catalog, and override ``candidate_options`` with real
grounded choices. The district notes below are the worked example; the metric
analogue (``apply_metric_clarification_grounding``) lives at the bottom of this
module and exists because a planner-direct metric clarify carries no typed
candidate set at all (the level names live only in the question prose), so the
planner echoes the single ambiguous phrase into ``candidates`` for this stage to
expand.


The planner can recognize a same-name district ambiguity at planning time (it
has catalog tools) and emit ``route="clarify"`` directly — authoring
``candidate_options`` itself with non-grounded prose values (e.g.
``value="Portland Public Schools, Portland, OR"``). That path never reaches
``execution.selection``'s grounded population, so the options aren't born from
catalog rows and a click can't deterministically resolve.

This module closes that gap: after the planner finalizes a district clarify,
re-resolve the district phrase through the catalog and **override**
``candidate_options`` with real grounded choices (``value=str(district_id)``,
state-qualified ``label``). The seed phrases are read from the planner's *typed*
clarify output (option labels / prose candidates) — not raw user prose — so this
is the allowed typed-referent-resolver shape, and the catalog is the grounding
authority (it returns real ``district_id``s or nothing).
"""

from __future__ import annotations

from compass_backend._clarify_helpers import _metric_clarification_option
from compass_backend.catalog import NcesDistrictMatch
from compass_backend.contracts.planning import (
    ClarificationOption,
    ClarificationRequest,
    PlannerTurn,
)
from compass_backend.execution.referent_resolution import split_state_suffix
from compass_backend.execution.selection import _district_clarification_option
from compass_backend.rendering.district_disambiguation import (
    grounded_uncovered_district_labels,
    grounded_uncovered_district_question,
)


def district_clarification_seed_phrases(
    clarification: ClarificationRequest,
) -> list[str]:
    """Distinct district phrases a clarify turn is asking about.

    Reads the planner's typed option labels and prose candidates (never raw user
    prose), stripping any trailing state qualifier so ``"Portland Public
    Schools, OR"`` and ``"Portland Public Schools"`` collapse to one phrase.
    """

    seeds: list[str] = []
    seen: set[str] = set()
    raw = [option.label for option in clarification.candidate_options]
    raw += list(clarification.candidates)
    for text in raw:
        name, _states = split_state_suffix(text)
        name = name.strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            seeds.append(name)
    return seeds


async def grounded_district_options(
    clarification: ClarificationRequest,
    catalog,
) -> list[ClarificationOption]:
    """Re-resolve the clarify's district phrases into grounded options.

    Each returned option carries a real ``district_id`` as its value and a
    state-qualified label, deduped by ``district_id`` across phrases.
    """

    grounded: list[ClarificationOption] = []
    seen_ids: set[int] = set()
    for phrase in district_clarification_seed_phrases(clarification):
        resolution = await catalog.resolve_districts([phrase])
        candidates = list(resolution.resolved)
        for bucket in resolution.ambiguous.values():
            candidates.extend(bucket)
        for candidate in candidates:
            if candidate.district_id in seen_ids:
                continue
            seen_ids.add(candidate.district_id)
            grounded.append(_district_clarification_option(candidate))
    return grounded


async def grounded_uncovered_clarification(
    clarification: ClarificationRequest,
    catalog,
) -> ClarificationRequest | None:
    """Re-ground a district clarify against the FULL NCES universe.

    Used only when covered resolution found nothing (the dominant none-covered
    case — e.g. "Cleveland County", where no Cleveland is in the Pathfinder).
    Searches NCES for each typed seed phrase, dedupes by ``leaid``, keeps the
    uncovered matches, and rebuilds the clarification with a grounded question
    ("…none are in the District Policy Pathfinder…") plus per-state candidate
    labels — never clickable chips (an uncovered district has no answerable
    ``district_id``). Returns ``None`` when NCES has nothing either, so the
    caller keeps today's drop-ungrounded behavior.
    """

    matches: list[NcesDistrictMatch] = []
    seen_leaids: set[str] = set()
    for phrase in district_clarification_seed_phrases(clarification):
        for match in await catalog.search_nces_districts(phrase):
            if match.leaid in seen_leaids:
                continue
            seen_leaids.add(match.leaid)
            matches.append(match)

    uncovered = [match for match in matches if not match.covered]
    labels = grounded_uncovered_district_labels(uncovered)
    if not labels:
        return None
    return clarification.model_copy(
        update={
            "question": grounded_uncovered_district_question(uncovered),
            "candidates": labels,
            "candidate_options": [],
        }
    )


async def apply_district_clarification_grounding(
    turn: PlannerTurn,
    catalog,
) -> PlannerTurn:
    """Return ``turn`` with its district-clarify options grounded.

    No-op unless the turn is a ``clarify`` whose ``missing_fields`` include
    ``"district"``. When grounding finds matches, model-authored
    ``candidate_options`` are replaced with grounded ones. When it finds nothing
    (rare — a hallucinated phrase), options are cleared rather than shipping
    ungrounded ones. Identity-returns the input when there's nothing to do.
    """

    clarification = turn.clarification
    if turn.route != "clarify" or clarification is None:
        return turn
    if "district" not in clarification.missing_fields:
        return turn
    if not clarification.candidate_options and not clarification.candidates:
        return turn

    grounded = await grounded_district_options(clarification, catalog)
    if not grounded:
        # No COVERED match. Before falling back to drop-ungrounded, try a
        # grounded NCES-wide disambiguation: the dominant case is a same-name
        # ambiguity where NONE of the candidates are covered (e.g. Cleveland),
        # which the covered-only resolution above can never surface. This names
        # the real districts by state and says plainly none are in the
        # Pathfinder — grounded rows replacing the planner's guess.
        nces_clarification = await grounded_uncovered_clarification(
            clarification, catalog
        )
        if nces_clarification is not None:
            return turn.model_copy(update={"clarification": nces_clarification})
        # NCES has nothing either (a genuinely unresolvable / hallucinated
        # phrase) — never ship ungrounded chips. Drop candidate_options; keep
        # prose `candidates` as a fallback.
        if not clarification.candidate_options:
            return turn
        return turn.model_copy(
            update={
                "clarification": clarification.model_copy(
                    update={"candidate_options": []}
                )
            }
        )
    # Override both surfaces with the grounded set so the chips, the prose
    # bullets, and the persisted memory all agree on the same grounded choices.
    grounded_clarification = clarification.model_copy(
        update={
            "candidate_options": grounded,
            "candidates": [option.label for option in grounded],
        }
    )
    return turn.model_copy(update={"clarification": grounded_clarification})


def metric_clarification_seed_phrases(
    clarification: ClarificationRequest,
) -> list[str]:
    """Distinct metric phrases a clarify turn is asking about.

    Reads the planner's typed option labels and prose candidates (never raw user
    prose). For a planner-direct metric clarify the planner echoes the single
    ambiguous phrase here (e.g. ``"planning time"``) — see planner.md — and the
    catalog expands it into the real candidate set.
    """

    seeds: list[str] = []
    seen: set[str] = set()
    raw = [option.label for option in clarification.candidate_options]
    raw += list(clarification.candidates)
    for text in raw:
        phrase = text.strip()
        key = phrase.casefold()
        if phrase and key not in seen:
            seen.add(key)
            seeds.append(phrase)
    return seeds


def _clarify_is_numeric_ranking(clarification: ClarificationRequest) -> bool:
    """Whether this clarify's pending request can only rank by a number.

    Read from the planner's TYPED ``pending_context`` (never the clarify prose).
    A ranking sorts districts by a numeric value, so a categorical/policy field
    ("permits/requires/prohibits…") can never be the thing to "rank by" — #1830.

    Ranking is recognized from any of three rank-shaped typed signals, so a
    partial mid-clarify context still trips the filter even if the planner left
    ``operation`` unset: ``operation == "rank"``, a top/bottom ``limit`` (a
    ranked cut like "the 5 …most…"), or a ``sort`` step. The planner-turn output
    validator guarantees a clarify carries ``pending_context``; when none of the
    rank signals is present this is ``False`` and no filter applies (a plain
    metric lookup still offers categorical policy fields).
    """

    context = clarification.pending_context
    if context is None:
        return False
    if context.operation == "rank":
        return True
    if context.limit is not None and context.limit.kind in {"top", "bottom"}:
        return True
    return context.sort is not None


async def grounded_metric_options(
    clarification: ClarificationRequest,
    catalog,
) -> list[ClarificationOption]:
    """Re-resolve the clarify's metric phrases into grounded options.

    Each phrase is expanded through the same catalog authority the executor
    uses (``resolve_metric_bundle``), so the offered options are born from real
    metric rows and carry the canonical, re-resolvable metric ``name`` as value.
    Deduped by ``metric_id`` across phrases.

    #1830: for a numeric-ranking clarify, exclude non-numeric (categorical /
    policy) candidates so "which metric to rank by" never offers a field that
    cannot be ranked. The catalog is asked with ``numeric_only`` AND the
    ``answer_type`` is re-checked here, because ``resolve_metric_bundle`` returns
    the RAW code-search set in ``.candidates`` (the flag governs ``.resolved`` /
    adjudication, not every branch's candidate list). Filtering on the typed
    ``answer_type`` field is grounding, not prose dispatch.
    """

    numeric_only = _clarify_is_numeric_ranking(clarification)
    grounded: list[ClarificationOption] = []
    seen_ids: set[int] = set()
    for phrase in metric_clarification_seed_phrases(clarification):
        resolution = await catalog.resolve_metric_bundle(
            phrase, numeric_only=numeric_only
        )
        for candidate in resolution.candidates:
            if numeric_only and (candidate.answer_type or "").casefold() != "numeric":
                continue
            if candidate.metric_id in seen_ids:
                continue
            seen_ids.add(candidate.metric_id)
            grounded.append(_metric_clarification_option(candidate))
    return grounded


async def apply_metric_clarification_grounding(
    turn: PlannerTurn,
    catalog,
) -> PlannerTurn:
    """Return ``turn`` with its planner-direct metric-clarify options grounded.

    The metric analogue of :func:`apply_district_clarification_grounding`. The
    planner can emit ``route="clarify"`` for an ambiguous metric phrase directly,
    but — unlike the execution-origin path, whose options are built at
    ``execution.selection``'s clarify site — it ships no structured options and,
    on the prose path, no typed candidate set at all (the level names live only
    in the question prose, which no-prose-dispatch forbids reading). So the
    planner echoes the single ambiguous metric phrase into ``candidates`` and
    this stage expands it through the catalog into real clickable options,
    overriding both surfaces so chips, prose, and persisted memory agree.

    No-op unless the turn is a non-rescue ``clarify`` whose ``missing_fields``
    include ``"metric"`` and that carries a seed phrase. Runs post-planner /
    pre-execution (orchestration.chat), so it never sees the execution-origin
    metric clarify. Identity-returns the input when there's nothing to ground.
    """

    clarification = turn.clarification
    if turn.route != "clarify" or clarification is None:
        return turn
    if "metric" not in clarification.missing_fields:
        return turn
    # #1613: a rescue-fallback clarify is a planner FAILURE, not a genuine
    # metric ambiguity — never decorate it with fabricated choices.
    if clarification.rescue_origin:
        return turn
    if not metric_clarification_seed_phrases(clarification):
        return turn

    grounded = await grounded_metric_options(clarification, catalog)
    if not grounded:
        # The phrase didn't resolve to catalog rows. Never ship ungrounded
        # chips: if the planner authored its own candidate_options, clear them
        # (keep prose candidates as a fallback) — mirroring the district
        # grounder's drop-ungrounded guard. Otherwise there's nothing to change.
        if not clarification.candidate_options:
            return turn
        return turn.model_copy(
            update={
                "clarification": clarification.model_copy(
                    update={"candidate_options": []}
                )
            }
        )
    # Override both surfaces so chips, prose bullets, and persisted memory all
    # agree on the same grounded, re-resolvable choices.
    grounded_clarification = clarification.model_copy(
        update={
            "candidate_options": grounded,
            "candidates": [option.label for option in grounded],
        }
    )
    return turn.model_copy(update={"clarification": grounded_clarification})
