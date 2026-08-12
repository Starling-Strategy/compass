"""Deterministic planner instruction snippets for Compass."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Protocol

from compass_backend.contracts.session import PlannerGuidanceEvidence
from compass_backend.instructions.loader import load_planner_guidance


@dataclass(frozen=True)
class PlannerInstructionSnippet:
    """Instruction-only planning guidance selected before the planner runs."""

    name: str
    filename: str
    trigger_phrases: tuple[str, ...]
    priority: int
    metadata: dict[str, str] = field(default_factory=dict)
    blocked_phrases: tuple[str, ...] = ()
    required_phrase_groups: tuple[tuple[str, ...], ...] = ()
    required_prior_route: str | None = None
    requires_query_context: bool = False
    exclusive: bool = False

    @property
    def body(self) -> str:
        """Return the static Markdown instruction body for this snippet."""

        return _snippet_text(self.filename)


@dataclass(frozen=True)
class PlannerInstructionSelection:
    """One selected planner instruction snippet plus why it matched."""

    name: str
    body: str
    metadata: dict[str, str] = field(default_factory=dict)
    matched_phrase: str | None = None

    def to_evidence(self) -> PlannerGuidanceEvidence:
        """Return the persisted, non-authoritative evidence form."""

        return PlannerGuidanceEvidence(
            name=self.name,
            metadata=self.metadata,
            matched_phrase=self.matched_phrase,
        )

    def model_payload(self) -> dict[str, object]:
        """Return the model-visible instruction payload."""

        payload: dict[str, object] = {
            "name": self.name,
            "instructions": self.body,
        }
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload


class PlannerInstructionDeps(Protocol):
    """Narrow planner deps surface used by the snippet selector."""

    message: str
    query_context: object | None
    # W0.5 (#832): the snippet selector needs only the routes of prior turns
    # (one snippet gates on ``required_prior_route``). The full transcript
    # prose moved to pydantic-ai message_history threaded into agent.run().
    recent_routes: tuple[str, ...]


FOLLOW_UP_REFERENCE = PlannerInstructionSnippet(
    name="follow-up-reference",
    filename="follow-up-reference.md",
    priority=10,
    trigger_phrases=(
        "those",
        "that",
        "this",
        "these",
        "them",
        "the results",
        "results",
        "chart",
        "export",
        "break down",
        "breakdown",
        "sort",
        # Multi-turn inheritance patterns (M3 #731, #732). Narrow set —
        # broader phrases (e.g., "they", "what are the") were tried but
        # induced false-positive selections that confused the planner on
        # cold-start follow-ups (regression: freestanding metric pick
        # following a count turn turned into an invalid plan). Stay specific.
        "in list above",
        "list above",
        "for those",
        "name them",
        "list them out",
        "list them",
        "the ones from",
        "the previous",
        "from earlier",
        "in the list",
        "from the list",
        "of the above",
        "of these",
    ),
    requires_query_context=True,
)

RANKING_AND_SORTING = PlannerInstructionSnippet(
    name="ranking-and-sorting",
    filename="ranking-and-sorting.md",
    priority=20,
    trigger_phrases=(
        "top",
        "bottom",
        "highest",
        "lowest",
        "largest",
        "smallest",
        "rank",
        "ranked",
        "ranking",
        "sort",
        "sorted",
        "order",
        "ordered",
    ),
)

COVERAGE_STATE_LANGUAGE = PlannerInstructionSnippet(
    name="coverage-state-language",
    filename="coverage-state-language.md",
    priority=25,
    trigger_phrases=(
        "availability",
        "available",
        "coverage",
        "covered",
        "data missing",
        "missing data",
        "not available",
        "no data",
        "not reviewed",
        "not applicable",
        "issue not addressed",
        "out of universe",
        "pathfinder",
        "older data",
        "prior year",
        "previous year",
        "not ranked",
    ),
)

TEACHER_COMPENSATION_SALARY = PlannerInstructionSnippet(
    name="teacher-compensation-salary",
    filename="teacher-compensation-salary.md",
    priority=30,
    trigger_phrases=(
        "salary",
        "salaries",
        "starting salary",
        "teacher salary",
        "teacher pay",
        "teachers make",
        "how much do teachers make",
        "pay teachers",
    ),
    blocked_phrases=(
        "does this well",
        "do this well",
        "does salary well",
        "do salary well",
        "exemplar",
        "exemplary",
        "model policy",
        "nctq recommend",
        "nctq's position",
        "research rationale",
        "research says",
    ),
    metadata={
        "topic_name": "Teacher Compensation",
        "subtopic_name": "Salary",
    },
)

TEACHER_EVALUATION_OBSERVATIONS = PlannerInstructionSnippet(
    name="teacher-evaluation-observations",
    filename="teacher-evaluation-observations.md",
    # Priority 31 — just after TEACHER_COMPENSATION_SALARY (30). Fires on
    # "observation count" phrases; the default-lane rule prevents a four-option
    # clarification when the user doesn't specify formal/informal or
    # tenured/non-tenured. Regression: case 14 (SORT-MIGRATED-285).
    priority=31,
    trigger_phrases=(
        "observation count",
        "observation counts",
        "observations per",
        "observations required",
        "required observations",
        "number of observations",
        "how many observations",
    ),
)

HEALTH_BENEFIT_EXEMPLAR = PlannerInstructionSnippet(
    name="health-benefit-exemplar",
    filename="health-benefit-exemplar.md",
    # Priority 15 — fires before TEACHER_COMPENSATION_SALARY (30) so the
    # "exemplary teacher benefits" shape never gets pulled into the salary
    # snippet's blocked-phrase territory. Equivalent recovery branch
    # previously lived in planning/answerability_recovery.py; retired here
    # per the M1 #1006 cleanup so phrase authority sits in governed
    # planner-snippet data, not below the planner.
    priority=15,
    trigger_phrases=(
        "great",
        "good",
        "strong",
        "best",
        "model",
        "exemplary",
        "does well",
    ),
    required_phrase_groups=(
        (
            "health coverage",
            "health benefits",
            "health insurance",
            "healthcare coverage",
            "health care coverage",
        ),
    ),
    blocked_phrases=(
        "how many",
        "count",
        "compare",
        "rank",
        "ten districts",
        "top 10",
        "top ten",
        "most",
        "least",
        "distribution",
        "each type",
        "premium",
        "specific metric",
    ),
    metadata={
        "topic_id": "benefits",
        "intent": "exemplar_request",
    },
)

COMPENSATION_SALARY_EXEMPLAR = PlannerInstructionSnippet(
    name="compensation-salary-exemplar",
    filename="compensation-salary-exemplar.md",
    # Priority 14 — fires before HEALTH_BENEFIT_EXEMPLAR (15) and
    # TEACHER_COMPENSATION_SALARY (30). Routes a bare subjective superlative
    # about pay ("best teacher pay", "leading on salaries", "make good money")
    # to general-salary exemplars instead of a which-salary-metric
    # clarification — the #1613 Mode-3 over-clarify family (cases 99/107/113/
    # 146-151), confirmed still live on main via local replay. ``exclusive``
    # so the broader TEACHER_COMPENSATION_SALARY snippet (which also matches the
    # salary phrase) does not also fire and pull the turn back toward an
    # execute/clarify shape. The salary required-group plus the ranking/
    # concrete blocked phrases keep concrete rankings and named-metric lookups
    # on the execute path.
    priority=14,
    exclusive=True,
    trigger_phrases=(
        "best",
        "good money",
        "leading",
        "leads the way",
        "the most",
        "great pay",
        "best job",
    ),
    required_phrase_groups=(
        (
            "teacher pay",
            "teacher salary",
            "teacher salaries",
            "salary",
            "salaries",
            "pay teachers",
            "teachers make",
            "make good money",
            "compensation",
        ),
    ),
    blocked_phrases=(
        "how many",
        "count",
        "rank",
        "ranked",
        "top 10",
        "top ten",
        "ten districts",
        "highest first",
        "lowest first",
        "list the",
        "more than",
        "greater than",
        "bachelor",
        "master",
        "maximum salary",
        "starting salary",
    ),
    metadata={
        "topic_id": "general-salary",
        "intent": "exemplar_request",
    },
)


# M1 #1006 part 2 — answerability_recovery retirements.
# Each of the three snippets below replaces a deterministic recovery branch
# that previously lived in planning/answerability_recovery.py. Phrase
# authority now sits in governed planner-snippet data; the planner LLM
# emits the typed PlannerTurn natively when the snippet body's required
# signals are present in the user message.
#
# The former HIGH_FRPL_STARTING_SALARY snippet was retired in #1088: it
# overlapped PROFILE_SORT_SALARY_DISPLAY (which owns the "salary display +
# profile-field sort" shape, including the highest-FRPL + starting-salary
# prompt) and additionally handles BA/MA lanes. Regression coverage lives in
# B-spine case REGR-M1-CLOSURE-FRPL-SALARY-RANK (case_id 1035).


PARENTAL_LEAVE_BEYOND_BIRTHING = PlannerInstructionSnippet(
    name="parental-leave-beyond-birthing",
    filename="parental-leave-beyond-birthing.md",
    # Priority 14 — fires before broader leave-related snippets. Three
    # required signals must all be present (parental-leave phrase, the
    # birthing-parent contrast point, and a "beyond" lane signal); narrow
    # by construction.
    priority=14,
    trigger_phrases=(
        "non-birthing",
        "nonbirthing",
        "non birthing",
        "adoptive",
        "foster",
        "beyond just",
        "more than just",
        "other than",
    ),
    required_phrase_groups=(
        ("paid parental leave",),
        ("birthing parent",),
    ),
    metadata={
        "intent": "parental_leave_beyond_birthing_lookup",
    },
)


SICK_LEAVE_DAY_RANKING = PlannerInstructionSnippet(
    name="sick-leave-ranking",
    filename="sick-leave-ranking.md",
    # Priority 16 — between PARENTAL_LEAVE_BEYOND_BIRTHING (14) and
    # SALARY_SCHEDULE_LOOKUP (17). Three required signals (a Texas scope, a
    # sick/leave-day phrase, and a ranking verb) must all be present, so it is
    # narrow by construction. Teaches the LLM the governed three-metric Texas
    # sick/leave-day ranking shape directly (the recipe-shaped half of #1060).
    priority=16,
    trigger_phrases=(
        "texas",
        "tx",
    ),
    required_phrase_groups=(
        ("sick leave", "paid leave", "sick days", "leave days"),
        ("day", "days"),
        (
            "highest",
            "most",
            "lowest",
            "least",
            "rank",
            "ranked",
            "ranking",
            "sort",
            "sorted",
            "compare",
        ),
    ),
    metadata={
        "intent": "texas_sick_leave_day_ranking",
    },
)


SALARY_SCHEDULE_LOOKUP = PlannerInstructionSnippet(
    name="salary-schedule-lookup",
    filename="salary-schedule-lookup.md",
    # Priority 17 — between HEALTH_BENEFIT_EXEMPLAR (15) and
    # DATA_INVENTORY (18). Fires before TEACHER_COMPENSATION_SALARY (30) so
    # the 3-metric overview shape wins on "X's salary schedule" prompts.
    priority=17,
    trigger_phrases=(
        "salary schedule",
        "salary schedules",
    ),
    blocked_phrases=(
        "compare",
        "rank",
        "highest",
        "top 10",
        "top ten",
        "across districts",
        "between",
        # Exemplar / policy-guidance intent ("a district that does this well",
        # "model salary schedule", "redesign") belongs to policy_guidance
        # exemplars, not a named-district salary-schedule lookup. See #1088.
        "does this well",
        "does it well",
        "redesign",
        "exemplary",
        "innovative",
        "front-load",
        "front load",
        "model salary schedule",
        "example of a district",
    ),
    metadata={
        "intent": "named_district_salary_schedule",
    },
)


DATA_INVENTORY = PlannerInstructionSnippet(
    name="data-inventory",
    filename="data-inventory.md",
    priority=18,
    trigger_phrases=(
        "what data do you have",
        "what info do you have",
        "what information do you have",
        "what data is available",
        "what data have you got",
    ),
    blocked_phrases=(
        "how many",
        "count",
        "compare",
        "rank",
        "top 10",
        "top ten",
    ),
    metadata={
        "intent": "data_inventory",
    },
)


DIFFERENTIATED_PAY_INVENTORY = PlannerInstructionSnippet(
    name="differentiated-pay-inventory",
    filename="differentiated-pay-inventory.md",
    # Priority 19 — between DATA_INVENTORY (18, which owns the "what data do you
    # have" capability shape) and RANKING_AND_SORTING (20). Fires on the
    # unspecified "differentiated pay" family phrase and teaches the governed
    # execute-inventory shape (region/state/district scope + one offer-anchor
    # metric per subtopic) so "differentiated pay in <region>" commits to a
    # useful answer instead of clarifying (#1017). A specific subtopic phrase
    # (e.g. "performance pay") does not contain "differentiated pay", so it
    # never matches here and falls through to normal catalog recognition.
    priority=19,
    trigger_phrases=(
        "differentiated pay",
        "differentiated-pay",
    ),
    blocked_phrases=(
        # Keep the "what data do you have on differentiated pay" capability
        # shape with DATA_INVENTORY, and count shapes with normal recognition.
        "what data",
        "what info",
        "what information",
        "how many",
        "count",
    ),
    metadata={
        "intent": "differentiated_pay_inventory",
    },
)


POLICY_GUIDANCE_ADVISORY_FOLLOWUP = PlannerInstructionSnippet(
    name="policy-guidance-advisory-followup",
    filename="policy-guidance-advisory-followup.md",
    # Priority 4 (below POLICY_GUIDANCE_FOLLOWUPS at 5) so an advisory
    # "should we prioritize pay or benefits?" turn pre-empts the
    # exemplar-detail follow-up snippet; both are exclusive (case 433, #1688).
    priority=4,
    trigger_phrases=(
        "prioritize",
        "prioritise",
        "matters more",
        "more important",
        "better to invest",
        "weigh",
    ),
    # Require the comparative "or" framing so a bare "should we prioritize
    # benefits?" (no second priority to weigh) does not steal the turn.
    required_phrase_groups=(("or",),),
    # Yield to the exemplar-detail / regional follow-up snippet when the user is
    # actually asking for details, sources, contract language, or a regional
    # narrowing of the prior exemplars — "which is more important, the source or
    # the contract language?" is a detail follow-up, not a policy weighing turn.
    blocked_phrases=(
        "details",
        "detail",
        "source",
        "sources",
        "contract",
        "narrow",
        "region",
    ),
    required_prior_route="policy_guidance",
    exclusive=True,
)

POLICY_GUIDANCE_FOLLOWUPS = PlannerInstructionSnippet(
    name="policy-guidance-followups",
    filename="policy-guidance-followups.md",
    priority=5,
    trigger_phrases=(
        "details",
        "source",
        "sources",
        "contract",
        "top",
        "that",
        "this",
        "one",
        # Regional refinement of a prior exemplar set ("narrow to the South",
        # "the Midwestern ones", "out West"). All four Census regions.
        "narrow",
        "region",
        "south",
        "southern",
        "north",
        "northeast",
        "northeastern",
        "midwest",
        "midwestern",
        "west",
        "western",
    ),
    required_prior_route="policy_guidance",
    exclusive=True,
)

PROFILE_SORT_SALARY_DISPLAY = PlannerInstructionSnippet(
    name="profile-sort-salary-display",
    filename="profile-sort-salary-display.md",
    priority=35,
    trigger_phrases=(
        "frpl",
        "lunch",
        "reduced",
    ),
    required_phrase_groups=(
        ("salary", "salaries", "pay"),
        (
            "highest",
            "top",
            "rank",
            "ranked",
            "sort",
            "sorted",
            "order",
            "ordered",
            "lowest",
            "bottom",
            "least",
            "fewest",
        ),
    ),
)

PROFILE_SORT_METRIC_DISPLAY = PlannerInstructionSnippet(
    name="profile-sort-metric-display",
    filename="profile-sort-metric-display.md",
    # Priority 22 — just after RANKING_AND_SORTING (20). Empirically tuned via
    # the #1315 probe so the "show <policy metric> for districts with the
    # highest <profile field>" shape lands in the selected top-3 (max_snippets=3)
    # alongside ranking-and-sorting (20) and teacher-compensation-salary (30).
    # Sits below ranking-and-sorting so the general ranking rule still leads, but
    # ahead of the salary topic snippet so the profile-ordered-display recipe is
    # never crowded out of the cap. Generalizes the FRPL-only
    # PROFILE_SORT_SALARY_DISPLAY (35) to any profile field (e.g. enrollment).
    priority=22,
    # Trigger on profile-field signals (not generic order words). Keeping generic
    # order words like "highest" out of the trigger set is what lets the FRPL
    # prompt ("highest free-and-reduced lunch share") fall through to
    # PROFILE_SORT_SALARY_DISPLAY without this snippet displacing it under the
    # 3-snippet cap. "largest"/"biggest" are scoped to "... district(s)" (word-
    # bounded) so a plain "largest starting salaries" — where "largest" modifies
    # the metric, not the district set — does NOT trip this snippet. See the
    # #1315 probe.
    trigger_phrases=(
        "enrollment",
        "largest district",
        "largest districts",
        "biggest district",
        "biggest districts",
    ),
    # The policy-metric required group is the GUARD: a bare "rank districts by
    # enrollment" carries no metric phrase, so this snippet does NOT fire and
    # RANKING_AND_SORTING still owns it. A sort-verb is also required so a flat
    # profile lookup never trips it.
    required_phrase_groups=(
        (
            "salary",
            "salaries",
            "pay",
            "stipend",
            "premium",
            "leave",
            "sick",
            "observation",
            "benefit",
            "benefits",
            "compensation",
        ),
        (
            "highest",
            "top",
            "rank",
            "ranked",
            "sort",
            "sorted",
            "order",
            "ordered",
            "lowest",
            "bottom",
            "least",
            "most",
            "largest",
        ),
    ),
    metadata={"intent": "profile_ordered_metric_display"},
)

PEER_SALARY_COMPARISON = PlannerInstructionSnippet(
    name="peer-salary-comparison",
    filename="peer-salary-comparison.md",
    priority=40,
    trigger_phrases=(
        "peer",
        "peers",
        "comparable",
        "similar",
    ),
    required_phrase_groups=(
        ("salary", "salaries", "pay"),
        ("maximum", "max"),
    ),
    metadata={"required_operation": "peer_comparison"},
)

# Priority 38 — runs before PEER_SALARY_COMPARISON (40) so the disambiguation
# rule fires first when both salary and peer/similar appear. PEER_SALARY_COMPARISON
# has required_phrase_groups that further narrow it to max-salary shape;
# SIMILARITY_DISCOVERY has no required_phrase_groups so it fires on any
# "peer / comparable / similar / enrollment outside" prompt.
SIMILARITY_DISCOVERY = PlannerInstructionSnippet(
    name="similarity-discovery",
    filename="similarity-discovery.md",
    priority=38,
    trigger_phrases=(
        "peer",
        "peers",
        "comparable",
        "similar",
        "similar districts",
        "enrollment outside",
        "find peers",
        "who are the peers",
        "peer districts",
        # "nearby districts" / "nearby schools" — geographic proximity requests
        # that don't use peer/similar language but still want covered neighbors.
        "nearby",
    ),
    # No blocked_phrases: when the user mentions a policy metric phrase (e.g.
    # "salaries", "maximum teacher salary") alongside "peer/comparable/similar",
    # SIMILARITY_DISCOVERY intentionally still fires so the LLM sees the mandatory
    # disambiguation rule in the snippet body.  Both SIMILARITY_DISCOVERY and
    # PEER_SALARY_COMPARISON fire together; the LLM reads both and correctly picks
    # operation="peer_comparison" per the disambiguation instruction.
    # Adding blocked_phrases like "salary" would prevent the LLM from seeing the
    # disambiguation rule on ambiguous prompts — defeating its purpose.
    # The ``required_phrase_groups`` on PEER_SALARY_COMPARISON (salary + max/maximum)
    # already narrows that snippet to the right shape; SIMILARITY_DISCOVERY's
    # disambiguation rule covers the remaining cases.
    blocked_phrases=(),
)

PEER_POLICY_COMPARISON = PlannerInstructionSnippet(
    name="peer-policy-comparison",
    filename="peer-policy-comparison.md",
    priority=41,
    trigger_phrases=(
        "peer",
        "peers",
        "comparable",
        "similar",
        "similar districts",
        "peer districts",
    ),
    required_phrase_groups=(
        (
            "policy",
            "policies",
            "sick leave",
            "leave",
            "benefit",
            "benefits",
            "premium",
            "stipend",
            "observation",
            "evaluation",
            "salary",
            "salaries",
            "pay",
            "compensation",
        ),
    ),
    metadata={"required_operation": "peer_comparison"},
)

DISTRICT_SPECIFIC_ABSENCE = PlannerInstructionSnippet(
    name="district-specific-absence",
    filename="district-specific-absence.md",
    # Priority 11 — just after ANCHOR_VALUE_FILTER (12) and before
    # FOLLOW_UP_REFERENCE (10). Fires on absence/inapplicability signals so the
    # district-level framing rule is injected before the planner (or renderer)
    # writes a state-level absence sentence. Narrow trigger set: collective
    # bargaining and right-to-work are the primary legal frameworks; "doesn't
    # apply" / "not applicable" are the verbal signals. No required_phrase_groups
    # — any one trigger is sufficient because the rule is universally applicable
    # whenever an absence claim is being narrated.
    priority=11,
    trigger_phrases=(
        "doesn't apply",
        "not applicable",
        "collective bargaining",
        "bargaining",
        "mandatory subjects",
        "right to work",
        "state law",
    ),
    metadata={"intent": "district_scoped_absence_narration"},
)

ANCHOR_VALUE_FILTER = PlannerInstructionSnippet(
    name="anchor-value-filter",
    filename="anchor-value-filter.md",
    # Priority 12 — ahead of SIMILARITY_DISCOVERY (38), PEER_POLICY_COMPARISON
    # (41), and PEER_SALARY_COMPARISON (40) so the "same VALUE as [anchor]"
    # equality shape is taught before the peer/similarity snippets. The bug
    # (#1485, case 1026): "districts with the same school-year length as
    # [anchor]" routed to operation="peer_comparison" and dead-ended on
    # "peer_comparison takes exactly one anchor district (received 6)". This is
    # an anchor-value equality filter (FilterSpec.anchor_value, kind="metric_value"),
    # not peer/similarity. Two required groups keep it narrow: a "same"/"match"
    # word AND a comparison connector — the bare "as" preposition token (which
    # captures "the same X **as** <any anchor>", the reporter's literal shape
    # "same school-year length as Portland, ME"), or a self/peer referent
    # ("we do", "they have"). The "as" connector is what makes the snippet fire
    # on a NAMED anchor we cannot enumerate as a phrase; the first group still
    # gates it so a sentence with "as" but no "same"/"match" word never trips
    # it. (Phrases are matched word-bounded — re.search with (?<!\w)…(?!\w) — so
    # the token is the bare word "as", not " as " with spaces, which would never
    # satisfy the leading boundary mid-sentence.)
    priority=12,
    trigger_phrases=(
        "same",
        "match",
        "matches",
        "matching",
    ),
    required_phrase_groups=(
        ("same", "match", "matches", "matching"),
        (
            "as",
            "we do",
            "we have",
            "they have",
            "they do",
        ),
    ),
    metadata={"intent": "anchor_value_equality_filter"},
)

PLANNER_INSTRUCTION_SNIPPETS = (
    POLICY_GUIDANCE_ADVISORY_FOLLOWUP,
    POLICY_GUIDANCE_FOLLOWUPS,
    ANCHOR_VALUE_FILTER,
    DISTRICT_SPECIFIC_ABSENCE,
    FOLLOW_UP_REFERENCE,
    PARENTAL_LEAVE_BEYOND_BIRTHING,
    SICK_LEAVE_DAY_RANKING,
    HEALTH_BENEFIT_EXEMPLAR,
    COMPENSATION_SALARY_EXEMPLAR,
    SALARY_SCHEDULE_LOOKUP,
    DATA_INVENTORY,
    DIFFERENTIATED_PAY_INVENTORY,
    RANKING_AND_SORTING,
    PROFILE_SORT_METRIC_DISPLAY,
    COVERAGE_STATE_LANGUAGE,
    TEACHER_COMPENSATION_SALARY,
    TEACHER_EVALUATION_OBSERVATIONS,
    PROFILE_SORT_SALARY_DISPLAY,
    SIMILARITY_DISCOVERY,
    PEER_POLICY_COMPARISON,
    PEER_SALARY_COMPARISON,
)


def select_planner_instruction_snippets(
    deps: PlannerInstructionDeps,
    *,
    max_snippets: int = 3,
) -> tuple[PlannerInstructionSelection, ...]:
    """Return deterministic planner instruction snippets for the current turn."""

    if max_snippets <= 0:
        return ()

    message = deps.message.lower()
    selected: list[PlannerInstructionSelection] = []
    for snippet in sorted(PLANNER_INSTRUCTION_SNIPPETS, key=lambda item: item.priority):
        if snippet.requires_query_context and deps.query_context is None:
            continue
        if snippet.required_prior_route is not None and not _has_prior_route(
            deps,
            snippet.required_prior_route,
        ):
            continue
        if _first_matching_phrase(message, snippet.blocked_phrases) is not None:
            continue
        matched_phrase = _first_matching_phrase(message, snippet.trigger_phrases)
        if matched_phrase is None:
            continue
        if not _matches_required_phrase_groups(message, snippet.required_phrase_groups):
            continue
        selected.append(
            PlannerInstructionSelection(
                name=snippet.name,
                body=snippet.body,
                metadata=dict(snippet.metadata),
                matched_phrase=matched_phrase,
            )
        )
        if snippet.exclusive or len(selected) >= max_snippets:
            break
    return tuple(selected)


def _has_prior_route(deps: PlannerInstructionDeps, route: str) -> bool:
    """Return whether any prior turn used the requested route."""

    return route in getattr(deps, "recent_routes", ())


def _matches_required_phrase_groups(
    message: str,
    groups: tuple[tuple[str, ...], ...],
) -> bool:
    """Return whether message matches at least one phrase from every group."""

    return all(_first_matching_phrase(message, group) is not None for group in groups)


def _first_matching_phrase(message: str, phrases: tuple[str, ...]) -> str | None:
    """Return the first matching phrase in stable snippet order."""

    for phrase in phrases:
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", message):
            return phrase
    return None


def _snippet_text(filename: str) -> str:
    """Load a packaged Markdown planner instruction snippet."""

    return load_planner_guidance(filename)
