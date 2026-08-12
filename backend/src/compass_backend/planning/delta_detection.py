"""Deterministic detection of multi-turn delta intent.

Pure-Python function: no DB calls, no LLM calls, no async. Mirrors the
prefilter pattern in quality/prefilters.py — a deterministic gate that runs
BEFORE the planner LLM call, narrowing the LLM's structural-output task.

When detect_delta_intent() returns a DeltaIntent, the caller passes the
signal into PlannerDeps.delta_intent. The planner agent's @agent.instructions
decorator then prepends a DELTA-INTENT CONSTRAINT block to the system prompt
requiring inherit_from_session=True. Telemetry records detector fires and
whether the planner honored the constraint.

The detector is conservative: false negatives just give us today's planner
behavior, while false positives could corrupt fresh queries by forcing
inheritance. Two guards keep this safe:

  1. memory.latest_query_context must be present — first-turn messages
     cannot trigger inheritance.
  2. Replacement/anaphora signals must actually be present in the user
     message; ambiguous phrasing falls through to today's planner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from compass_backend.contracts.session import ConversationMemory

# Numeric "the/those/these N" referent — extracts the count the user named
# ("the 90", "those 90 districts", "the 19"). Covers the same referent surface
# as _LIST_THOSE_N_REFERENTS (so a paraphrase like "those 90" is captured) AND
# the bare "the 90" that _LIST_THOSE_N_REFERENTS misses. It does NOT require a trailing
# "districts". To stay safe it only counts as a population referent when the
# captured N equals a real number from the prior count (its denominator) — see
# _classify_present_target. That gate keeps "the 3 observations" inert.
_THE_N_NUMERIC_RE = re.compile(
    r"\b(?:the|those|these)\s+(\d[\d,]*)(?:\s+districts?)?\b"
)

# Population (denominator-membership) phrasing — a narrow SECONDARY signal for a
# follow-up that wants the count's selected population, not its matching subset.
# The detector lives in the planning layer (above the no-prose-dispatch boundary,
# AGENTS.md guardrail 4), so reading the user's phrasing here is legal.
#
# Two unambiguous shapes only:
#   1. the "denominator" keyword.
#   2. an ANAPHORIC "(have|has|with) [a] [current] data/value (on|for) this/it/that"
#      — the anaphor ("this"/"it"/"that") scopes the ask to the PRIOR count's
#      metric (the count just shown), so it requests that count's data-having
#      population. This is the reporter's literal "...districts that have data on
#      this?" (case 393). A bare/fresh "have data on <new topic>" has NO anaphor
#      and does NOT match, so it cannot hijack a fresh universe question after a
#      count (see test_generic_have_data_phrasing_does_not_hijack_fresh_query).
# Bare "have data"/"with data" with no anaphor is still NOT a population referent.
_POPULATION_PHRASE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("the denominator", re.compile(r"\bdenominator\b")),
    (
        "have data on this",
        re.compile(
            r"\b(?:have|has|having|with)\s+(?:a\s+)?(?:current\s+)?"
            r"(?:data|value|values)\s+(?:on|for)\s+(?:this|it|that)\b"
        ),
    ),
)

# Anaphoric markers — bounded phrases that reference a prior result set.
_REFERENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("those", re.compile(r"\bthose\b")),
    ("them", re.compile(r"\bthem\b")),
    ("these", re.compile(r"\bthese\b")),
    ("that order", re.compile(r"\bthat\s+order\b")),
    ("that list", re.compile(r"\bthat\s+list\b")),
    ("the same", re.compile(r"\bthe\s+same\b")),
    ("same five", re.compile(r"\bsame\s+(?:five|5)\b")),
)

# Strong list-those-N referents — fire on their own (no verb required) because
# the referent + prior context is sufficient evidence of a "list these specific
# rows" follow-up. Added 2026-05-28 to close #937 / #1011 (the planner LLM is
# not reliably emitting inherit_selection_from="prior_result_rows" for the
# "What are the 17 districts and how many observations do they require?" shape;
# firing the detector here adds the DELTA-INTENT CONSTRAINT block to the system
# prompt, which mandates the typed referent).
_LIST_THOSE_N_REFERENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("the N districts", re.compile(r"\bthe\s+\d+\s+districts?\b")),
    ("those N districts", re.compile(r"\bthose\s+\d+\s+districts?\b")),
    ("those N", re.compile(r"\bthose\s+\d+\b")),
)

# Bare "name them" follow-ups (#1665, root cause (b): routing).
# After a count answers a NUMBER ("77 districts"), the user says "name them" with
# no digit, so none of the digit-requiring _LIST_THOSE_N_REFERENTS match and the
# detector returned None — the LLM then free-emits a bare named_districts plan
# that collides at resolve_districts into the Portland ME/OR ambiguity block.
# This deterministic shape closes that gap: a name/list VERB plus a prior-result
# ANAPHORA (see _NAME_LIST_ANAPHORA — the pure pronouns "them"/"all of them" and
# the qualifier-guarded "the districts") references the prior result rows, so it
# inherits + lists them ("Name, ST"-qualified via _named_districts_selection). It
# NEVER fires on a first-turn request like "name 5 districts with sick leave" —
# that has the verb but no prior-result anaphora (the digit + topic is a fresh
# selection, not a referent).
_NAME_LIST_VERBS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("name", re.compile(r"\bname\b")),
    ("list", re.compile(r"\blist\b")),
)

# Anaphora that pair with a name/list verb to reference the prior result rows.
# The split is pronoun vs. determiner, because only pronouns are unambiguously
# anaphoric:
#   - "them" / "all of them" are pure pronouns — they can ONLY refer to the
#     prior set, so they are safe unguarded.
#   - "the districts" is a definite noun phrase that is a prior-result referent
#     only when it stands alone ("name the districts"); a trailing fresh
#     qualifier makes it a NEW selection ("name the districts in Texas",
#     "list the districts with paid family leave"). The negative lookahead
#     `(?!\s+\w)` fires on "the districts" / "the districts?" / "the district"
#     but not "the districts in …".
#   - "those" is DELIBERATELY ABSENT. As a demonstrative determiner it routinely
#     introduces a fresh selection ("list those districts in Texas", "name those
#     top 10 districts by enrollment"), and the trailing-qualifier guard can't
#     save it — it can't tell anaphoric "name those districts" (the prior ones)
#     from fresh "name those Texas districts". Dropping it keeps the conservative
#     posture: "name those" falls through to the soft LLM follow-up snippet
#     rather than risk overwriting a fresh re-query with stale prior rows
#     (the #1665-review over-fire regression class).
_NAME_LIST_ANAPHORA: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("them", re.compile(r"\bthem\b")),
    ("the districts", re.compile(r"\bthe\s+districts?\b(?!\s+\w)")),
    ("all of them", re.compile(r"\ball\s+of\s+them\b")),
)

# Aggregate-count question cues. A "how many of those N ..." ask wants a NUMBER
# over the inherited set, not the set enumerated — so it must NOT be forced into
# a list even though it carries a list-those-N referent (C2, #1414).
_COUNT_QUESTION_CUES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bhow\s+many\b"),
    re.compile(r"\bhow\s+much\b"),
)

# Bare anaphora that only count as a prior-result referent INSIDE a count
# question (#1211). The reporter's literal failing prompt — "how many districts
# is that?" right after Compass listed a set — uses a bare "that"/"those"/
# "these"/"them" with no list-N referent (the "N" count is in the prior turn,
# not restated) and no operation verb, so the detector returned None and the
# planner refused ("I want to make sure I...") instead of counting the set it
# had just shown. A bare "that" is too weak to treat as a referent on its own —
# it collides with relative-clause uses ("districts that pay above $50k") — so
# it is gated behind a count cue AND prior context: a count question whose only
# referent is a bare prior-result pronoun is unambiguously a "count what you
# just showed me" follow-up. It feeds matched_anaphora + has_count_question=True,
# so the inherited rows are counted, never force-listed (mirrors the
# "how many of those N" path that already inherits and counts).
_COUNT_BARE_REFERENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("that", re.compile(r"\bthat\b")),
    ("those", re.compile(r"\bthose\b")),
    ("these", re.compile(r"\bthese\b")),
    ("them", re.compile(r"\bthem\b")),
)

# Operation verbs that imply replacement of the prior order/set with a referent.
_SELF_REPLACING_VERBS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("reverse", re.compile(r"\breverse\b")),
    ("flip", re.compile(r"\bflip\b")),
    ("reorder", re.compile(r"\breorder\b")),
    ("re-order", re.compile(r"\bre-order\b")),
    ("re-sort", re.compile(r"\bre-sort\b")),
)

# Operation verbs that need a replacement marker to imply a delta.
_NEEDS_REPLACEMENT_VERBS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sort", re.compile(r"\bsort\b")),
    ("rank", re.compile(r"\brank\b")),
    ("order", re.compile(r"\border\b")),
)

# Markers that signal replacement vs addition.
_REPLACEMENT_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("instead", re.compile(r"\binstead\b")),
    ("rather than", re.compile(r"\brather\s+than\b")),
    ("by", re.compile(r"\bby\b")),
)


@dataclass(frozen=True)
class DeltaIntent:
    """A detected delta-intent signal for the planner."""

    matched_anaphora: tuple[str, ...]
    matched_verbs: tuple[str, ...]
    has_prior_query: bool
    # The list-those-N referents specifically (subset of matched_anaphora),
    # kept separate so the planner can structurally force a list presentation
    # on a bare "name/list the inherited set" ask (C2, #1414).
    matched_list_referents: tuple[str, ...] = ()
    # A "how many of those N ..." aggregate-count question — present means the
    # user wants a number, not the set enumerated, so don't force a list.
    has_count_question: bool = False
    # Whether the prior turn carried a metric the forced list can inherit. A
    # metric-less prior (e.g. a covered-universe count) has no value to list,
    # so forcing a lookup there would refuse rather than enumerate.
    prior_has_metric: bool = False
    # Which side of a count's selection the follow-up wants to PRESENT (#393).
    # "matches" (default) is the qualifying subset (the prior list-those-N
    # behavior); "population" is the count's denominator members — covered
    # districts with a current value — for a "who are the <denominator>?" /
    # "who has data?" follow-up. Routed only off grounded prior-count sizes (and
    # a phrasing fallback), never below the planning boundary.
    present_target: Literal["matches", "population"] = "matches"
    # A digit-free "name them" / "list those" follow-up (#1665). Like a
    # list-those-N referent it enumerates the prior RESULT ROWS, but it carries
    # no number to match, so it is its own typed flag (no synthetic referent
    # injected into matched_list_referents — mirrors the present_target idiom).
    fired_name_list_referent: bool = False

    def is_name_list_intent(self) -> bool:
        """True for a bare 'name/list the inherited set' follow-up.

        A list-those-N referent ("the 18 districts", "those 12") with no
        sort/reorder verb and no aggregate-count question — the user wants the
        prior set ENUMERATED, not reshaped or re-aggregated. Sort/reverse deltas
        and "how many of those" asks are excluded. A population-target ask
        ("who are the 90?") is likewise an enumeration intent even when its
        bare "the 90" carries no list-those-N referent. A bare "name them" /
        "list those" (#1665) is the same enumeration intent without a number.
        """

        if self.matched_verbs or self.has_count_question:
            return False
        return (
            bool(self.matched_list_referents)
            or self.present_target == "population"
            or self.fired_name_list_referent
        )


def detect_delta_intent(
    user_message: str,
    memory: ConversationMemory,
) -> DeltaIntent | None:
    """Return DeltaIntent if user message looks like a delta on prior context.

    Three conditions (all-of):
      1. memory.latest_query_context is not None (something to delta from).
      2. user_message contains a bounded prior-result referent, plus either
         a self-replacing verb (reverse/flip/reorder/re-order/re-sort) or a
         replacement marker (instead/rather than/by).
      3. (Implicit from 1+2) the combination is consistent with a
         sort/filter/limit delta.

    None on no fire — falls back to today's planner behavior.
    """
    if memory.latest_query_context is None:
        return None

    msg = user_message.casefold()
    matched_anaphora = tuple(
        label for label, pattern in _REFERENT_PATTERNS if pattern.search(msg)
    )
    matched_list_referents = tuple(
        label for label, pattern in _LIST_THOSE_N_REFERENTS if pattern.search(msg)
    )

    has_count_question = any(pattern.search(msg) for pattern in _COUNT_QUESTION_CUES)

    # #1211: a count question that references a bare prior-result pronoun
    # ("how many districts is that?", "how many of them?") inherits + counts the
    # prior rows. Bare pronouns are only honored as a self-firing referent inside
    # a count question — the gate that disarms relative-clause false positives
    # like "districts that pay above $50k". This also lets the existing weak
    # anaphora ("them"/"these") fire on their own when wrapped in a count
    # question, which on its own (no verb, no replacement marker) the second gate
    # below would otherwise reject.
    bare_count_referents = (
        tuple(
            label
            for label, pattern in _COUNT_BARE_REFERENT_PATTERNS
            if pattern.search(msg)
        )
        if has_count_question
        else ()
    )
    fired_count_bare_referent = bool(bare_count_referents)
    if fired_count_bare_referent:
        matched_anaphora = tuple(
            dict.fromkeys(matched_anaphora + bare_count_referents)
        )

    # #393: decide whether the follow-up wants the count's POPULATION (its
    # denominator members) or its MATCHES (the qualifying subset). Grounded on
    # the prior count's sizes, not phrasing: a numeric "the N" that equals the
    # population (and not the matched count) routes to population; a phrasing
    # fallback ("who has data") routes there when no numeric N pins it. Both
    # require a prior count that actually carried a population. The numeric path
    # also lets a BARE "the 90" fire the detector even when it lacks the
    # "districts"/"those" that _LIST_THOSE_N_REFERENTS demands.
    # MINOR 5a: present_target ("population" vs "matches") is the ONE typed
    # encoding of a denominator ask — no synthetic referent string is injected
    # into matched_list_referents. The two fire-gates below admit it via
    # fired_population_referent, and is_name_list_intent() reads present_target
    # directly, so the population intent never has to masquerade as a list-those-N
    # referent.
    present_target, fired_population_referent = _classify_present_target(
        msg, memory
    )

    # #1665: a digit-free "name them" / "list those" / "name the districts"
    # follow-up. A name/list VERB plus a prior-result ANAPHORA (them/those, or
    # the literal "the districts" / "all of them") is sufficient evidence of an
    # enumerate-the-prior-rows ask — the same intent as a list-those-N referent
    # but with no number to match. It is gated on the verb AND the anaphora so a
    # first-turn "name 5 districts with sick leave" (verb, no anaphora) stays
    # inert. It fires on its own (admitted by both gates below) and routes to the
    # MATCHES via prior_result_rows (the count's result rows carry state, so the
    # ME/OR duplicate disambiguates in _named_districts_selection).
    fired_name_list_verb = any(
        pattern.search(msg) for _, pattern in _NAME_LIST_VERBS
    )
    fired_name_list_anaphora = any(
        pattern.search(msg) for _, pattern in _NAME_LIST_ANAPHORA
    )
    fired_name_list_referent = fired_name_list_verb and fired_name_list_anaphora

    if (
        not matched_anaphora
        and not matched_list_referents
        and not fired_population_referent
        and not fired_name_list_referent
    ):
        return None

    matched_self_replacing = tuple(
        label for label, pattern in _SELF_REPLACING_VERBS if pattern.search(msg)
    )
    matched_needs_replacement = tuple(
        label for label, pattern in _NEEDS_REPLACEMENT_VERBS if pattern.search(msg)
    )
    has_replacement_marker = any(
        pattern.search(msg) for _, pattern in _REPLACEMENT_MARKERS
    )

    matched_verbs = matched_self_replacing + (
        matched_needs_replacement if has_replacement_marker else ()
    )

    # Strong list-those-N referents, the #1211 count-bare-referent, the #393
    # population referent, and the #1665 "name them" referent fire on their own;
    # the referent + prior context is sufficient evidence of an
    # operate-on-the-prior-rows follow-up.
    if (
        not matched_list_referents
        and not matched_verbs
        and not has_replacement_marker
        and not fired_count_bare_referent
        and not fired_population_referent
        and not fired_name_list_referent
    ):
        return None

    prior_plan = memory.latest_query_context.query_plan
    prior_has_metric = bool(prior_plan.metrics or prior_plan.profile_fields)

    return DeltaIntent(
        matched_anaphora=matched_anaphora + matched_list_referents,
        matched_verbs=matched_verbs,
        has_prior_query=True,
        matched_list_referents=matched_list_referents,
        has_count_question=has_count_question,
        prior_has_metric=prior_has_metric,
        present_target=present_target,
        fired_name_list_referent=fired_name_list_referent,
    )


def _classify_present_target(
    msg: str,
    memory: ConversationMemory,
) -> tuple[Literal["matches", "population"], bool]:
    """Decide population-vs-matches for a count follow-up (#393).

    Returns ``(present_target, fired_population_referent)``. The boolean is True
    only when the population path is what made the message a referent at all
    (a bare numeric "the 90" or a "who has data" phrase), so the caller knows to
    let it fire the detector on its own.

    Grounded discriminator (strong signal):
      * numeric "the N" where N == size(prior population) and != size(prior
        matches) → population.
      * numeric "the N" where N == size(prior matches) → matches (the existing
        list-those-N behavior; never hijack it).
    Phrasing fallback (secondary): a population phrase — the "denominator"
    keyword or an anaphoric "have data on this/it/that" — → population. It is
    reached when no numeric N is present AND when a cited number matched NEITHER
    the population nor the matches (a stale/approximate count must not veto a
    clear phrase — data drifts year to year).

    All population routing requires a prior count that carried a population —
    a non-zero ``count_denominator`` (the #1658-reliable integer) or, for
    pre-#1658 memory, a non-empty ``result_population_districts``; otherwise it
    stays "matches".
    """
    context = memory.latest_query_context
    if context is None:
        return "matches", False

    # #1658: ground on the denominator INTEGER (reliably persisted), falling back
    # to the member-list length only when the integer is absent (pre-#1658 memory).
    # The member list is intermittently dropped in serialization (~12%); grounding
    # on it made "who are the 90?" misfire to matches when it was lost.
    population_size = (
        context.count_denominator
        if context.count_denominator is not None
        else len(context.result_population_districts)
    )
    matched_size = len(context.result_districts)
    has_population = population_size > 0

    numeric_match = _THE_N_NUMERIC_RE.search(msg)
    if numeric_match is not None:
        n = int(numeric_match.group(1).replace(",", ""))
        if has_population and n == population_size and n != matched_size:
            return "population", True
        if n == matched_size:
            # The user cited the MATCH count exactly → matches; never hijack the
            # existing list-those-N path.
            return "matches", False
        # The cited number equals NEITHER the population nor the matches. Data
        # shifts year to year (a sync moved the count from 90 -> 96 after these
        # fixtures were written), so an approximate/stale number must NOT veto a
        # clear population phrase: fall through to the phrasing check below rather
        # than returning matches here. "Who are the 90 districts that have data on
        # this?" against a 96-district population still routes to population on the
        # anaphoric phrase, instead of the matched subset.

    # Phrasing fallback — also reached when a non-matching number was cited above.
    # Only fires when a population exists to present.
    if has_population and any(
        pattern.search(msg) for _, pattern in _POPULATION_PHRASE_PATTERNS
    ):
        return "population", True

    return "matches", False


__all__ = ["DeltaIntent", "detect_delta_intent"]
