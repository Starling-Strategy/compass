"""Pure-Python renderer for the policy_guidance planner route.

Reads a typed ``PolicyGuidanceBundle`` and emits a ``ResponseManifest`` —
no LLM call, no parametric-memory fallback. The prose comes verbatim from
the markdown-loaded library; the LLM upstream picked ``topic_ids`` and
``layers`` (both Literal-bound), but never authors content here. This is
the strongest defense against fabrication in user-facing answers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from compass_backend.contracts import PolicyGuidancePlan
from compass_backend.contracts.rendering import ResponseManifest
from compass_backend.policy_guidance.models import (
    ExemplarPolicy,
    PATHFINDER_PLACEHOLDER,
    PolicyGuidanceBundle,
    PolicyGuidanceLibrary,
    ResearchRationale,
    Stance,
    TopicGuidance,
)
from compass_backend.reference import census_region_label, expand_state_or_region_values

# Topic-generic no-stance paragraph. The first sentence states the boundary
# plainly; the second offers a concrete on-ramp to areas NCTQ *does* research,
# rather than the older "Compass focuses on policy areas..." restatement that
# named no real research area (a non-pivot that report-card criterion penalized
# as misleading). The named areas (teacher pay, evaluation, leave) assume
# collective bargaining is the only no-stance topic today; revisit if another
# no-stance topic is added.
_NO_POLICY_STANCE_PARAGRAPH = (
    "NCTQ does not take a policy stance on {topic_name}. "
    "If it helps, you can ask about areas NCTQ does research and evaluate, "
    "such as teacher pay, evaluation, and leave."
)

_LAYER_HEADINGS: dict[str, str] = {
    "stances": "NCTQ's Position",
    "rationales": "Research Basis",
    "exemplars": "Exemplary Districts",
}
POLICY_GUIDANCE_VALIDATION_FAILED_BODY = (
    "I couldn't safely render this NCTQ guidance response because its citation "
    "metadata did not match the rendered text."
)
# Visible inline marker shown in the body of any bullet whose citation entry
# has citation_status == "placeholder" (Pathfinder homepage stand-in). Until
# real per-topic canonical URLs land (SSN-254), readers must see that the
# linked source is a stopgap, not a real published page.
PROVISIONAL_SOURCE_MARKER = "[provisional source]"
_BODY_MARKER_RE = re.compile(r"\[((?:\d+)|(?:stance|rationale|exemplar):[^\]\s]+)\]")


@dataclass(frozen=True)
class PolicyGuidanceManifestValidation:
    valid: bool
    findings: tuple[str, ...]


def _strong_policy_framing_line(topic: TopicGuidance) -> str | None:
    """One grounded sentence naming what NCTQ looks for in a strong policy for
    this topic, built from its policy-stance titles (#1626 §1).

    Prefixes an exemplars-only answer so it teaches the framework before the
    example districts, instead of jumping straight to a list. GROUNDED — the
    titles come verbatim from the library's stances; no criteria are invented.
    Returns ``None`` when the topic carries no policy stance (nothing to
    summarize — e.g. a no-policy-stance topic).
    """

    titles = [s.title for s in topic.stances if s.kind == "policy_stance"]
    if not titles:
        return None
    if len(titles) == 1:
        joined = titles[0]
    elif len(titles) == 2:
        joined = f"{titles[0]} and {titles[1]}"
    else:
        joined = f"{', '.join(titles[:-1])}, and {titles[-1]}"
    return (
        f"What NCTQ looks for in a strong {topic.canonical_topic.lower()} "
        f"policy: {joined}."
    )


def render_policy_guidance(
    plan: PolicyGuidancePlan,
    bundle: PolicyGuidanceBundle,
    *,
    library: PolicyGuidanceLibrary,
) -> ResponseManifest:
    """Render NCTQ policy guidance prose from the typed bundle."""
    bundle, focus_fallback_layers = _filter_bundle_by_focus_terms(
        plan, bundle, library=library
    )
    bundle = _filter_bundle_by_selected_exemplars(plan, bundle)
    region_states = _region_member_states(plan)
    if region_states is not None:
        bundle = _filter_exemplars_by_states(bundle, region_states)

    if not (bundle.stances or bundle.rationales or bundle.exemplars):
        if region_states is not None:
            # The region refinement filtered every exemplar out. Say so against
            # the governed region rather than the generic "couldn't find" miss —
            # the topic exists, we just have no curated in-region exemplar.
            return _no_region_match_manifest(plan, library=library)
        no_stance_manifest = _no_stance_only_manifest(plan, library=library)
        if no_stance_manifest is not None:
            return no_stance_manifest
        return _empty_bundle_manifest(plan)

    ordered_topic_ids = _order_topic_ids(plan)
    multi_topic = len(ordered_topic_ids) > 1
    multi_layer = len(plan.layers) > 1

    # Identify topics whose stances layer is entirely no-policy-stance so we can
    # exclude those stances from the citation index (they render as prose, not bullets).
    no_stance_topic_ids: set[str] = {
        tid
        for tid in ordered_topic_ids
        if _is_all_no_policy_stance(
            [s for s in bundle.stances if s.topic_id == tid]
        )
    }
    # Build a citation-filtered bundle that omits no-policy-stance stances for topics
    # that will use the dedicated paragraph rendering path.
    citation_bundle = _bundle_without_no_stance_topics(bundle, no_stance_topic_ids)

    citations, marker_by_stable_id, placeholder_stable_ids = _build_citation_index(
        citation_bundle,
        library=library,
        ordered_topic_ids=ordered_topic_ids,
        layers=list(plan.layers),
    )

    # When the exemplars layer fell back (focus_terms matched nothing there and
    # we kept the broad set), frame the intro against the canonical topic rather
    # than the unmatched subtopic phrase, so the heading matches what's shown.
    parts: list[str] = [
        _intro_for_plan(
            plan,
            library=library,
            bundle=bundle,
            suppress_focus_label="exemplars" in focus_fallback_layers,
        )
    ]

    for topic_id in ordered_topic_ids:
        topic = library.topics[topic_id]
        topic_parts: list[str] = []
        if multi_topic:
            topic_parts.append(f"## {topic.canonical_topic}")

        # Inject no-policy-stance paragraph before other layers when applicable.
        if topic_id in no_stance_topic_ids and "stances" in plan.layers:
            topic_parts.append(
                _NO_POLICY_STANCE_PARAGRAPH.format(topic_name=topic.canonical_topic)
            )

        # #1626 §1: a "who has the best/strongest <topic>?" answer renders only
        # the exemplars layer and jumps straight to example districts. Lead with
        # one GROUNDED sentence naming what NCTQ looks for in a strong policy
        # (from the topic's stance titles) so the user gets the principle before
        # the list. Only when the stances layer is NOT itself rendered (else it
        # duplicates the Position section).
        if "exemplars" in plan.layers and "stances" not in plan.layers:
            framing = _strong_policy_framing_line(topic)
            if framing is not None:
                topic_parts.append(framing)

        for layer in plan.layers:
            if layer == "stances" and topic_id in no_stance_topic_ids:
                # Already handled via the dedicated paragraph above; skip bullet render.
                continue
            section = _render_layer(
                layer,
                topic_id,
                bundle,
                marker_by_stable_id=marker_by_stable_id,
                placeholder_stable_ids=placeholder_stable_ids,
            )
            if section is None:
                continue
            if multi_layer:
                heading_level = "###" if multi_topic else "##"
                topic_parts.append(f"{heading_level} {_LAYER_HEADINGS[layer]}")
            topic_parts.append(section)

        if topic_parts:
            parts.append("\n\n".join(topic_parts))

    if plan.response_mode == "advisory_comparison":
        # Close the weigh-two-priorities frame opened by the intro: present the
        # stacked guidance as decision input, not a ranking. Decision framing
        # only — asserts no new NCTQ stance beyond what the sections carry.
        parts.append(
            "Rather than choosing one over the other, weigh the positions and "
            "research above to decide which lever your district most needs."
        )

    candidate_body = "\n\n".join(parts)
    validation = validate_policy_guidance_manifest(candidate_body, citations)
    body = candidate_body if validation.valid else POLICY_GUIDANCE_VALIDATION_FAILED_BODY

    warnings = list(validation.findings) + [
        f"focus_terms_unproductive:{layer}" for layer in focus_fallback_layers
    ]

    return ResponseManifest(
        body=body,
        status="rendered" if validation.valid else "validation_failed",
        result_type="policy_guidance",
        validation_valid=validation.valid,
        warnings=warnings,
        metadata={
            "topic_ids": list(plan.topic_ids),
            "layers": list(plan.layers),
            "primary_topic_id": plan.primary_topic_id,
            "intent_summary": plan.intent_summary,
            "focus_terms": list(plan.focus_terms),
            "selected_exemplar_ids": list(plan.selected_exemplar_ids),
            "response_mode": plan.response_mode,
            "region": plan.region,
            "citations": citations,
        },
    )


# ---------------------------------------------------------------------------
# Helpers


def _focus_terms_restate_topic(
    terms: tuple[str, ...],
    plan: PolicyGuidancePlan,
    library: PolicyGuidanceLibrary,
) -> bool:
    """True iff every focus term merely restates the topic itself.

    A term restates the topic when it overlaps (either direction) the topic's
    id, canonical name, or one of its catalog aliases — i.e. the planner echoed
    the topic instead of naming a narrower subtopic. Used to decide whether an
    unproductive ``focus_terms`` filter should fall back to the broad topic
    (planner restated the topic) or remain an honest empty (genuine subtopic the
    library doesn't cover).
    """
    identities: set[str] = set()
    for topic_id in plan.topic_ids:
        topic = library.topics.get(topic_id)
        if topic is None:
            continue
        identities.add(topic_id.casefold())
        identities.add(topic.canonical_topic.casefold())
        identities.update(alias.casefold() for alias in topic.aliases)
    identities = {identity for identity in identities if identity}
    if not identities:
        return False

    def _restates(term: str) -> bool:
        return any(identity in term or term in identity for identity in identities)

    return all(_restates(term) for term in terms)


def _filter_bundle_by_focus_terms(
    plan: PolicyGuidancePlan,
    bundle: PolicyGuidanceBundle,
    *,
    library: PolicyGuidanceLibrary,
) -> tuple[PolicyGuidanceBundle, tuple[str, ...]]:
    """Apply typed subtopic narrowing to broad-topic guidance bundles.

    ``focus_terms`` is a *narrowing* hint. When it matches some items in a layer
    we keep only those (genuine subtopic narrowing — e.g. "parental leave"
    within Leave).

    When it matches **nothing** in a populated layer, the outcome depends on
    what the term is:

    - If the term merely **restates the topic itself** (its id, canonical name,
      or an alias — e.g. ``["teacher evaluation"]`` for the evaluation topic),
      it's an unproductive hint, not a request to show nothing. Wiping the layer
      would surface a false "couldn't find content" dead-end, so we keep the
      layer unfiltered and report it.
    - If the term is a **genuine narrower subtopic** the library doesn't cover
      (e.g. ``["strike legality"]`` within Leave), an empty result is the honest
      answer — we do NOT substitute unrelated exemplars.

    Returns ``(filtered_bundle, fell_back_layers)`` where ``fell_back_layers``
    names the layers whose topic-redundant narrowing was discarded, so the
    caller can surface a ``focus_terms_unproductive`` warning and frame the
    intro against the broad topic instead of the unmatched subtopic.
    """
    terms = tuple(term.strip().lower() for term in plan.focus_terms if term.strip())
    if not terms:
        return bundle, ()

    # Only rescue a wipeout when the terms are just the topic restated; a
    # genuine unsupported subtopic stays an honest empty.
    topic_redundant = _focus_terms_restate_topic(terms, plan, library)
    fell_back: list[str] = []

    def _guard(layer_name: str, original: tuple, filtered: tuple) -> tuple:
        if original and not filtered and topic_redundant:
            fell_back.append(layer_name)
            return original
        return filtered

    stances = _guard(
        "stances",
        bundle.stances,
        tuple(
            stance
            for stance in bundle.stances
            if _matches_focus_terms(terms, stance.stance_id, stance.title, stance.body)
        ),
    )
    rationales = _guard(
        "rationales",
        bundle.rationales,
        tuple(
            rationale
            for rationale in bundle.rationales
            if _matches_focus_terms(
                terms,
                rationale.rationale_id,
                rationale.stance_id,
                rationale.title,
                rationale.body,
                rationale.source_title,
            )
        ),
    )
    exemplars = _guard(
        "exemplars",
        bundle.exemplars,
        tuple(
            exemplar
            for exemplar in bundle.exemplars
            if _matches_focus_terms(
                terms,
                exemplar.exemplar_id,
                exemplar.district,
                exemplar.subtopic,
                exemplar.body,
            )
        ),
    )

    return (
        PolicyGuidanceBundle(
            topic_ids=bundle.topic_ids,
            layers=bundle.layers,
            stances=stances,
            rationales=rationales,
            exemplars=exemplars,
        ),
        tuple(fell_back),
    )


def _filter_bundle_by_selected_exemplars(
    plan: PolicyGuidancePlan,
    bundle: PolicyGuidanceBundle,
) -> PolicyGuidanceBundle:
    """Restrict rendered exemplars to a deterministic selected-id list."""
    selected_ids = tuple(
        exemplar_id.strip()
        for exemplar_id in plan.selected_exemplar_ids
        if exemplar_id.strip()
    )
    if not selected_ids:
        return bundle

    exemplar_by_id = {exemplar.exemplar_id: exemplar for exemplar in bundle.exemplars}
    selected_exemplars = tuple(
        exemplar_by_id[exemplar_id]
        for exemplar_id in selected_ids
        if exemplar_id in exemplar_by_id
    )
    return PolicyGuidanceBundle(
        topic_ids=bundle.topic_ids,
        layers=bundle.layers,
        stances=bundle.stances,
        rationales=bundle.rationales,
        exemplars=selected_exemplars,
    )


def _region_member_states(plan: PolicyGuidancePlan) -> frozenset[str] | None:
    """Expand ``plan.region`` to governed Census member-state codes.

    Returns ``None`` when no region refinement is requested (the filter is a
    no-op). Returns a possibly-empty frozenset when a region phrase is set —
    an empty set means the phrase isn't a governed region, so no exemplar can
    match it and the caller emits the honest no-in-region-match response. The
    expansion is delegated to the same ``reference.regions`` table the data
    query path uses, so "the South" means the identical governed state set in
    both surfaces.
    """
    region = (plan.region or "").strip()
    if not region:
        return None
    return frozenset(expand_state_or_region_values([region]))


def _filter_exemplars_by_states(
    bundle: PolicyGuidanceBundle,
    states: frozenset[str],
) -> PolicyGuidanceBundle:
    """Keep only exemplars whose ``state`` is in the governed region set.

    Stances and rationales are topic-level (not district-level), so they pass
    through untouched. An exemplar with an unknown state is dropped under an
    active region filter — we won't claim it's in-region without evidence.
    """
    kept = tuple(
        exemplar
        for exemplar in bundle.exemplars
        if exemplar.state is not None and exemplar.state in states
    )
    if kept == bundle.exemplars:
        return bundle
    return PolicyGuidanceBundle(
        topic_ids=bundle.topic_ids,
        layers=bundle.layers,
        stances=bundle.stances,
        rationales=bundle.rationales,
        exemplars=kept,
    )


def _region_display_label(region: str) -> str:
    """Human-readable framing for a governed region phrase ("the South").

    Delegates to the canonical region table so all four Census regions render
    consistently; falls back to the cleaned phrase for anything ungoverned
    (which can only reach a label path defensively — ungoverned phrases filter
    to no in-region match upstream).
    """
    label = census_region_label(region)
    if label is not None:
        return label
    cleaned = " ".join(region.strip().split())
    if cleaned.lower().startswith("the "):
        return cleaned
    return f"the {cleaned}"


def _matches_focus_terms(terms: tuple[str, ...], *parts: str) -> bool:
    haystack = " ".join(parts).lower()
    return any(term in haystack for term in terms)


def _order_topic_ids(plan: PolicyGuidancePlan) -> list[str]:
    """Move ``primary_topic_id`` (if set) to the front; preserve plan order."""
    ordered = list(plan.topic_ids)
    primary = plan.primary_topic_id
    if primary is not None and primary in ordered:
        ordered.remove(primary)
        ordered.insert(0, primary)
    return ordered


def _focus_term_label(term: str) -> str:
    """Format a focus_term for display in the intro heading.

    Capitalizes the first letter of each space-separated word while preserving
    internal casing — so "parental leave" → "Parental Leave" but
    "hard-to-staff schools" → "Hard-to-staff Schools" (not Hard-To-Staff via
    `.title()`) and "ELL teacher" → "ELL Teacher" (uppercase acronym preserved).
    """
    return " ".join(
        (word[:1].upper() + word[1:]) if word else word
        for word in term.strip().split()
    )


def _intro_for_plan(
    plan: PolicyGuidancePlan,
    *,
    library: PolicyGuidanceLibrary,
    bundle: PolicyGuidanceBundle,
    suppress_focus_label: bool = False,
) -> str:
    """Deterministic framing derived from approved topics and requested layers.

    When `plan.focus_terms` is non-empty (subtopic narrowing — e.g. "parental
    leave" within the broader "Leave" topic), the heading uses the focus
    terms so the user sees subtopic-specific framing matching the filtered
    bundle. Falls back to canonical topic labels for broad requests. Closes
    #737.

    `suppress_focus_label` forces canonical topic framing even when focus_terms
    is set — used when the focus_terms narrowing was unproductive on exemplars
    and we fell back to the broad set, so the heading matches what's shown.
    """
    if plan.focus_terms and not suppress_focus_label:
        topic_labels = [_focus_term_label(term) for term in plan.focus_terms]
    else:
        topic_labels = [
            library.topics[topic_id].canonical_topic
            for topic_id in _order_topic_ids(plan)
            if topic_id in library.topics
        ]
    topic_text = _format_topic_list(topic_labels or list(plan.topic_ids))
    # When a governed regional refinement is active and we're surfacing
    # exemplars, name the region in the framing so the geographic filter is
    # explicit ("... for Performance Pay in the South") rather than silent.
    if plan.region and (
        "exemplars" in plan.layers or plan.response_mode == "exemplar_detail"
    ):
        topic_text = f"{topic_text} in {_region_display_label(plan.region)}"
    if plan.response_mode == "advisory_comparison":
        # Weigh-two-priorities follow-up ("should we prioritize pay or
        # benefits?"). Frame the stacked stance/rationale sections as decision
        # input for a not-strictly-either/or question rather than letting them
        # read as two parallel topic dumps (criterion 59 / SCENARIO_FIT). This
        # is framing only — the substance stays the governed library content.
        return (
            f"NCTQ doesn't frame {topic_text} as an either/or — both shape how "
            "districts recruit and keep strong teachers. Here's NCTQ's position "
            "and the research behind each, so you can weigh them for your district:"
        )
    if plan.response_mode == "exemplar_detail":
        if len(bundle.exemplars) == 1:
            return (
                "Here are the approved policy details for "
                f"{bundle.exemplars[0].district} on {topic_text}."
            )
        return (
            "Here are the approved policy details for the selected exemplary "
            f"districts on {topic_text}."
        )
    layers = list(plan.layers)
    if layers == ["exemplars"]:
        # #1613: General Salary exemplar requests come from a subjective
        # superlative ("who has the best teacher pay?") where users expect a
        # ranking. Lead with a brief subjectivity frame so the answer reads as
        # NCTQ's editorial picks, not an objective "best". Scoped to
        # general-salary — benefits/perf-pay/evaluation exemplar leads pass
        # today and the subjective-ranking confusion is compensation-specific.
        salary_subjectivity = (
            'There\'s no single "best" on teacher pay — it depends what you '
            "value. "
            if "general-salary" in plan.topic_ids
            else ""
        )
        # #936: when only one exemplar survives bundle filters (focus_terms,
        # selected_exemplar_ids, post-#972 context retention), the plural
        # "policies" framing reads as a comprehensive list and trips the
        # uncertainty_acknowledgment judge ~300×/week on policy_guidance.
        # Explicit "one" gives the judge the count-acknowledgment cue.
        if len(bundle.exemplars) == 1:
            return (
                f"{salary_subjectivity}Here is one NCTQ-curated exemplary "
                f"district policy for {topic_text}."
            )
        return (
            f"{salary_subjectivity}Here are NCTQ-curated exemplary district "
            f"policies for {topic_text}."
        )
    if layers == ["stances"]:
        return f"Here is NCTQ's position on {topic_text}."
    if layers == ["rationales"]:
        return f"Here is NCTQ's research basis for {topic_text}."
    return f"Here is NCTQ policy guidance for {topic_text}."


def _format_topic_list(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{', '.join(labels[:-1])}, and {labels[-1]}"


def _is_all_no_policy_stance(stances: list[Stance]) -> bool:
    """Return True iff stances is non-empty and every stance is kind == 'no_policy_stance'."""
    return bool(stances) and all(s.kind == "no_policy_stance" for s in stances)


def _bundle_without_no_stance_topics(
    bundle: PolicyGuidanceBundle,
    no_stance_topic_ids: set[str],
) -> PolicyGuidanceBundle:
    """Return a bundle with no-policy-stance stances removed for topics in the given set.

    Used so the citation index excludes stances that render as the dedicated
    no-policy-stance paragraph rather than as citation-bearing bullets.
    """
    if not no_stance_topic_ids:
        return bundle
    return PolicyGuidanceBundle(
        topic_ids=bundle.topic_ids,
        layers=bundle.layers,
        stances=tuple(
            s for s in bundle.stances if s.topic_id not in no_stance_topic_ids
        ),
        rationales=bundle.rationales,
        exemplars=bundle.exemplars,
    )


def _render_layer(
    layer: str,
    topic_id: str,
    bundle: PolicyGuidanceBundle,
    *,
    marker_by_stable_id: Mapping[str, str],
    placeholder_stable_ids: frozenset[str],
) -> str | None:
    """Render one layer's items for one topic. Returns None if nothing matches."""
    if layer == "stances":
        items = [s for s in bundle.stances if s.topic_id == topic_id]
        return (
            _render_stances(
                items,
                marker_by_stable_id=marker_by_stable_id,
                placeholder_stable_ids=placeholder_stable_ids,
            )
            if items
            else None
        )
    if layer == "rationales":
        items_r = [r for r in bundle.rationales if r.topic_id == topic_id]
        return (
            _render_rationales(
                items_r,
                marker_by_stable_id=marker_by_stable_id,
                placeholder_stable_ids=placeholder_stable_ids,
            )
            if items_r
            else None
        )
    if layer == "exemplars":
        items_e = [e for e in bundle.exemplars if e.topic_id == topic_id]
        return (
            _render_exemplars(
                items_e,
                marker_by_stable_id=marker_by_stable_id,
                placeholder_stable_ids=placeholder_stable_ids,
            )
            if items_e
            else None
        )
    return None


def _format_bullet(
    *,
    label: str,
    body: str,
    stable_id: str,
    marker_by_stable_id: Mapping[str, str],
    placeholder_stable_ids: frozenset[str],
) -> str:
    """Build one bullet line; prefix the body with PROVISIONAL_SOURCE_MARKER
    iff the citation index entry for ``stable_id`` is a placeholder."""
    prefixed_body = (
        f"{PROVISIONAL_SOURCE_MARKER} {body}"
        if stable_id in placeholder_stable_ids
        else body
    )
    return (
        f"- **{label}** — {prefixed_body} "
        f"{_citation_marker(marker_by_stable_id, stable_id)}"
    )


def _render_stances(
    stances: list[Stance],
    *,
    marker_by_stable_id: Mapping[str, str],
    placeholder_stable_ids: frozenset[str],
) -> str:
    """One stance per bullet; display marker after the body for citation."""
    lines = [
        _format_bullet(
            label=stance.title,
            body=stance.body,
            stable_id=stance.stance_id,
            marker_by_stable_id=marker_by_stable_id,
            placeholder_stable_ids=placeholder_stable_ids,
        )
        for stance in stances
    ]
    return "\n".join(lines)


def _render_rationales(
    rationales: list[ResearchRationale],
    *,
    marker_by_stable_id: Mapping[str, str],
    placeholder_stable_ids: frozenset[str],
) -> str:
    lines = [
        _format_bullet(
            label=rationale.title,
            body=rationale.body,
            stable_id=rationale.rationale_id,
            marker_by_stable_id=marker_by_stable_id,
            placeholder_stable_ids=placeholder_stable_ids,
        )
        for rationale in rationales
    ]
    return "\n".join(lines)


def _render_exemplars(
    exemplars: list[ExemplarPolicy],
    *,
    marker_by_stable_id: Mapping[str, str],
    placeholder_stable_ids: frozenset[str],
) -> str:
    """District name leads; body verbatim from markdown; numeric marker.

    Body sometimes already starts with the district name (per the markdown
    style) — we still prefix with bold district to keep the bullet shape
    consistent and visible at a glance.
    """
    lines = [
        _format_bullet(
            label=exemplar.district,
            body=exemplar.body,
            stable_id=exemplar.exemplar_id,
            marker_by_stable_id=marker_by_stable_id,
            placeholder_stable_ids=placeholder_stable_ids,
        )
        for exemplar in exemplars
    ]
    return "\n".join(lines)


def _citation_marker(
    marker_by_stable_id: Mapping[str, str],
    stable_id: str,
) -> str:
    return marker_by_stable_id.get(stable_id, f"[{stable_id}]")


def validate_policy_guidance_manifest(
    body: str,
    citations: list[Mapping[str, object]],
) -> PolicyGuidanceManifestValidation:
    """Validate body citation markers against manifest metadata.

    The renderer is artifact-first: if marker metadata and the body diverge,
    the manifest is explicitly invalid instead of silently exposing citations
    that the frontend may misrepresent.
    """

    body_markers = {match.group(0) for match in _BODY_MARKER_RE.finditer(body)}
    metadata_markers: set[str] = set()
    findings: list[str] = []

    for index, citation in enumerate(citations):
        citation_id = citation.get("id")
        if type(citation_id) is not int or citation_id <= 0:
            findings.append(f"invalid_citation_id:index={index}")
            expected_marker = None
        else:
            expected_marker = f"[{citation_id}]"

        marker = citation.get("marker")
        if not isinstance(marker, str) or not marker:
            findings.append(f"missing_citation_marker:index={index}")
            continue
        if expected_marker is not None and marker != expected_marker:
            findings.append(
                f"citation_marker_id_mismatch:index={index}:marker={marker}:id={citation_id}"
            )
        if marker in metadata_markers:
            findings.append(f"duplicate_metadata_citation:marker={marker}")
        metadata_markers.add(marker)

        stable_id = citation.get("stable_id")
        if not isinstance(stable_id, str) or not stable_id:
            findings.append(f"missing_stable_id:marker={marker}")

        if marker not in body_markers:
            findings.append(f"metadata_citation_not_in_body:marker={marker}")

        status = citation.get("citation_status")
        if status is None:
            findings.append(f"missing_citation_status:marker={marker}")
        elif status not in {"ready", "placeholder"}:
            findings.append(f"invalid_citation_status:marker={marker}:status={status}")

    for marker in sorted(body_markers - metadata_markers):
        findings.append(f"missing_metadata_citation:marker={marker}")

    return PolicyGuidanceManifestValidation(
        valid=not findings,
        findings=tuple(findings),
    )


def _build_citation_index(
    bundle: PolicyGuidanceBundle,
    *,
    library: PolicyGuidanceLibrary,
    ordered_topic_ids: list[str],
    layers: list[str],
) -> tuple[list[dict[str, object]], dict[str, str], frozenset[str]]:
    """Build the citation list + body-side resolver maps for the renderer.

    Each Sources entry has:
    - ``id`` and ``marker`` — the numeric display citation the body contains
    - ``stable_id`` — the catalog ID of the first row that introduced this URL
    - ``url`` — the clickable source link (Pathfinder for placeholders;
      teacherquality.nctq.org or similar for exemplars)
    - ``title`` — a short human-readable label for the panel

    Returns ``(citations, marker_by_stable_id, placeholder_stable_ids)``:
    - ``citations`` carries one entry per unique URL identity (first-seen
      wins) so the Sources panel shows one row per document.
    - ``marker_by_stable_id`` maps EVERY input stable_id — including aliases
      whose URL was deduplicated against an earlier entry — to the merged
      ``"[N]"`` marker so body bullets render the correct number.
    - ``placeholder_stable_ids`` carries every stable_id whose merged Sources
      entry is a placeholder, so aliased bullets still surface
      "[provisional source]" consistently with the first-seen bullet.

    Mirrors ``execution/evidence.py:_citation_identity`` (PR #868 / #748)
    for the dedup key: URL casefold when present, parsed-title casefold
    fallback. Page-anchor precision is sacrificed in favour of one Sources
    entry per URL — first-seen anchor wins.
    """
    citations: list[dict[str, object]] = []
    marker_by_stable_id: dict[str, str] = {}
    placeholder_stable_ids: set[str] = set()
    marker_by_identity: dict[tuple[object, ...], str] = {}
    citation_by_identity: dict[tuple[object, ...], dict[str, object]] = {}

    def register(citation: dict[str, object]) -> None:
        url = str(citation.get("url") or "").strip()
        title = str(citation.get("title") or "").strip()
        identity: tuple[object, ...] = (
            ("url", url.casefold()) if url else ("title", title.casefold())
        )
        existing_marker = marker_by_identity.get(identity)
        if existing_marker is None:
            next_id = len(citations) + 1
            existing_marker = f"[{next_id}]"
            citation["id"] = next_id
            citation["marker"] = existing_marker
            marker_by_identity[identity] = existing_marker
            citation_by_identity[identity] = citation
            citations.append(citation)
        stable_id = citation.get("stable_id")
        if isinstance(stable_id, str) and stable_id:
            marker_by_stable_id[stable_id] = existing_marker
            kept = citation_by_identity[identity]
            if kept.get("citation_status") == "placeholder":
                placeholder_stable_ids.add(stable_id)

    for topic_id in ordered_topic_ids:
        for layer in layers:
            if layer == "stances":
                for stance in [s for s in bundle.stances if s.topic_id == topic_id]:
                    topic = library.topics[stance.topic_id]
                    url = (
                        str(topic.canonical_url)
                        if topic.canonical_url is not None
                        else PATHFINDER_PLACEHOLDER
                    )
                    citation_status = (
                        "ready" if topic.canonical_url is not None else "placeholder"
                    )
                    register(
                        {
                            "stable_id": stance.stance_id,
                            "citation_type": "stance",
                            "topic_id": stance.topic_id,
                            "source_kind": (
                                "topic_canonical_url"
                                if topic.canonical_url is not None
                                else "topic_placeholder"
                            ),
                            "citation_status": citation_status,
                            "url": url,
                            "title": f"NCTQ stance: {stance.title}",
                        }
                    )
            elif layer == "rationales":
                for rationale in [
                    r for r in bundle.rationales if r.topic_id == topic_id
                ]:
                    register(
                        {
                            "stable_id": rationale.rationale_id,
                            "citation_type": "rationale",
                            "topic_id": rationale.topic_id,
                            "stance_id": rationale.stance_id,
                            "source_kind": "research_rationale",
                            "citation_status": rationale.citation_status,
                            "url": str(rationale.source_url),
                            "title": f"{rationale.source_title}: {rationale.title}",
                        }
                    )
            elif layer == "exemplars":
                for exemplar in [e for e in bundle.exemplars if e.topic_id == topic_id]:
                    register(
                        {
                            "stable_id": exemplar.exemplar_id,
                            "citation_type": "exemplar",
                            "topic_id": exemplar.topic_id,
                            "district": exemplar.district,
                            "district_id": exemplar.district_id,
                            "subtopic": exemplar.subtopic,
                            "source_kind": "district_exemplar",
                            "citation_status": exemplar.citation_status,
                            "url": str(exemplar.source_url),
                            "title": f"{exemplar.district} — {exemplar.subtopic}",
                        }
                    )
    return citations, marker_by_stable_id, frozenset(placeholder_stable_ids)


def _no_stance_only_manifest(
    plan: PolicyGuidancePlan,
    *,
    library: PolicyGuidanceLibrary,
) -> ResponseManifest | None:
    """Route a known no-policy-stance topic to the no-position paragraph.

    Reached only when the requested-layer bundle is empty (#1218). A topic such
    as collective bargaining is in the library and carries a no-policy-stance
    stance, but has no exemplars/rationales — so asking for an *exemplar* yields
    an empty bundle that would otherwise read as a "couldn't find" search miss.
    The topic is out of scope by design, not missing, so emit the typed
    no-position prose instead.

    Returns ``None`` (caller falls through to the honest-empty message) unless
    EVERY requested topic that exists in the library is all-no-policy-stance and
    at least one such topic is present — so a topic that genuinely takes a
    stance but lacks content for the requested layer still gets the honest miss.
    Routing keys off the typed ``Stance.kind`` in the library, not user prose.
    """
    ordered_topic_ids = [
        tid for tid in _order_topic_ids(plan) if tid in library.topics
    ]
    if not ordered_topic_ids:
        return None
    no_stance_topic_ids = [
        tid
        for tid in ordered_topic_ids
        if _is_all_no_policy_stance(list(library.topics[tid].stances))
    ]
    if len(no_stance_topic_ids) != len(ordered_topic_ids):
        return None

    multi_topic = len(no_stance_topic_ids) > 1
    parts: list[str] = []
    for topic_id in no_stance_topic_ids:
        topic = library.topics[topic_id]
        if multi_topic:
            parts.append(f"## {topic.canonical_topic}")
        parts.append(
            _NO_POLICY_STANCE_PARAGRAPH.format(topic_name=topic.canonical_topic)
        )
    body = "\n\n".join(parts)

    return ResponseManifest(
        body=body,
        status="rendered",
        result_type="policy_guidance",
        validation_valid=True,
        warnings=[f"no_policy_stance_topics:{no_stance_topic_ids}"],
        metadata={
            "topic_ids": list(plan.topic_ids),
            "layers": list(plan.layers),
            "primary_topic_id": plan.primary_topic_id,
            "intent_summary": plan.intent_summary,
            "focus_terms": list(plan.focus_terms),
            "selected_exemplar_ids": list(plan.selected_exemplar_ids),
            "response_mode": plan.response_mode,
            "region": plan.region,
            "citations": [],
        },
    )


def _empty_bundle_manifest(plan: PolicyGuidancePlan) -> ResponseManifest:
    """Honest fallback when the requested layers are empty for these topics."""
    layers_str = ", ".join(plan.layers)
    topics_str = ", ".join(plan.topic_ids)
    body = (
        f"I couldn't find NCTQ {layers_str} content for {topics_str} in the "
        f"current policy library. Try a different layer (stances, rationales, "
        f"exemplars) or ask about another topic."
    )
    return ResponseManifest(
        body=body,
        status="rendered",
        result_type="policy_guidance",
        validation_valid=True,
        warnings=[f"empty_bundle:topics={list(plan.topic_ids)}:layers={list(plan.layers)}"],
        metadata={
            "topic_ids": list(plan.topic_ids),
            "layers": list(plan.layers),
            "primary_topic_id": plan.primary_topic_id,
            "intent_summary": plan.intent_summary,
            "focus_terms": list(plan.focus_terms),
            "selected_exemplar_ids": list(plan.selected_exemplar_ids),
            "response_mode": plan.response_mode,
            "region": plan.region,
            "citations": [],
        },
    )


def _no_region_match_manifest(
    plan: PolicyGuidancePlan,
    *,
    library: PolicyGuidanceLibrary,
) -> ResponseManifest:
    """Honest response when a region refinement filters out every exemplar.

    Distinct from ``_empty_bundle_manifest`` so the user hears that the topic
    exists but has no curated exemplary district in the requested region —
    rather than a generic "couldn't find content" that reads as a data gap.
    """
    region_label = _region_display_label(plan.region or "")
    if plan.focus_terms:
        topic_labels = [_focus_term_label(term) for term in plan.focus_terms]
    else:
        topic_labels = [
            library.topics[topic_id].canonical_topic
            for topic_id in _order_topic_ids(plan)
            if topic_id in library.topics
        ] or list(plan.topic_ids)
    topic_text = _format_topic_list(topic_labels)
    body = (
        f"NCTQ doesn't have a curated exemplary district policy in {region_label} "
        f"for {topic_text} in the approved library. You can broaden the region or "
        f"ask to see the full set of exemplary districts."
    )
    return ResponseManifest(
        body=body,
        status="rendered",
        result_type="policy_guidance",
        validation_valid=True,
        warnings=[f"no_region_match:region={plan.region!r}:topics={list(plan.topic_ids)}"],
        metadata={
            "topic_ids": list(plan.topic_ids),
            "layers": list(plan.layers),
            "primary_topic_id": plan.primary_topic_id,
            "intent_summary": plan.intent_summary,
            "focus_terms": list(plan.focus_terms),
            "selected_exemplar_ids": list(plan.selected_exemplar_ids),
            "response_mode": plan.response_mode,
            "region": plan.region,
            "citations": [],
        },
    )
