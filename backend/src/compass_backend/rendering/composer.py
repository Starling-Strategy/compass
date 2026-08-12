"""Deterministic answer composition for validated Compass artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from compass_backend.artifacts import (
    REVIEWED_NO_FIGURE_STATES,
    CountRow,
    CoverageDisclosure,
    DistrictCoverageSummary,
    FilterPrevalenceSummary,
    LargestExcludedDistrict,
    ListCoverageSummary,
    ResultSelection,
    ResultSelectionScope,
    ResultSet,
    coverage_label_for_out_of_universe,
    promoted_row_disclosures,
    short_coverage_label,
)
from compass_backend.contracts.planning import QueryPlan
from compass_backend.execution.selection import requested_enrollment_bounds
from compass_backend.execution.selection import requested_states as _requested_states
from compass_backend.execution.selection import split_unresolved_district_state
from compass_backend.observability import log_info
from compass_backend.reference import (
    normalize_state as _normalize_state,
)
from compass_backend.reference import (
    state_full_name as _state_full_name,
)
from compass_backend.rendering.formatting import (
    MIN_DENOMINATOR_FOR_PERCENT,
    format_percent,
)
from compass_backend.rendering.methodology import methodology_line_for
from compass_backend.rendering.shared import selection_label, sort_metric_name


_DETAILED_COVERAGE_DISCLOSURE_LIMIT = 5

# Coverage line is suppressed when the coverage rate is at or above this fraction.
# Below 70% the Coverage line adds material context not already in the lead paragraph;
# above 70% it duplicates the "I found current reviewed data for N of M cells" lead.
_COVERAGE_VERBOSE_THRESHOLD = 0.7


# #1651: maps the FilterSpec.operator onto the plain-language cutoff direction the
# transparency note states. "regularly" → greater_than_or_equal → "or more"; the
# rare strict / upper-bound forms are spelled too so the note never lies about the
# applied comparison.
_THRESHOLD_OPERATOR_PHRASE = {
    "greater_than_or_equal": "or more",
    "greater_than": "or more",
    "less_than_or_equal": "or less",
    "less_than": "or less",
    "equals": "exactly",
}


def _format_threshold_value(value: object, *, currency: bool) -> str | None:
    """Format a scalar FilterSpec.value as the cutoff number the user reads.

    Returns ``None`` for non-scalar / boolean values so the note degrades to the
    phrase-only form rather than printing a meaningless token. ``currency`` is
    derived from the rendered rows (#1651) so the cutoff's format matches the
    column it filters ("$5,000" for a dollar metric, "2" for a count metric).
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if currency:
        return f"${value:,.0f}"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:,}"


def _result_is_currency(result: ResultSet | None) -> bool:
    """True when the rendered rows display dollar values (#1651).

    Reads the same ``display_value`` strings the user sees in the table, so the
    cutoff number is formatted like the column it filters without re-deriving
    currency-ness from the metric phrase.
    """

    if result is None:
        return False
    rows = getattr(result, "rows", None) or ()
    for row in rows:
        display_value = getattr(row, "display_value", None)
        if isinstance(display_value, str) and display_value.strip().startswith("$"):
            return True
    return False


def _threshold_hint_line(plan: QueryPlan, result: ResultSet | None = None) -> str | None:
    """Return a transparency note when any filter carries a vague-quantifier hint.

    #1651: names the concrete cutoff Compass applied (FilterSpec.value, formatted
    to match the rendered column) alongside the user's phrase, so a district just
    below the line is not silently dropped. Formatted as:
    "(interpreting '<hint>' as <number> <direction>)" — e.g.
    "(interpreting 'real (not token)' as $5,000 or more)". Falls back to the
    phrase-only form when the filter carries no scalar value (the renderer cannot
    invent a number it was not given).

    The line opens on the sealed "interpreting" marker so the answer-layer voice
    pass preserves it (briefs._CANONICAL_CAVEAT_LINE_MARKERS) — mirroring the
    "match your criteria" seal that protects the #1337 denominator lead.

    Returns the first non-None threshold_hint found across plan.filters; None when
    no filters carry a threshold_hint.
    """
    currency = _result_is_currency(result)
    for filter_spec in plan.filters:
        hint = filter_spec.threshold_hint
        if not hint:
            continue
        number = _format_threshold_value(filter_spec.value, currency=currency)
        direction = _THRESHOLD_OPERATOR_PHRASE.get(filter_spec.operator)
        if number is not None and direction is not None:
            return f"(interpreting '{hint}' as {number} {direction})"
        return f"(interpreting '{hint}' from your question)"
    return None


@dataclass
class AnswerComposition:
    """User-facing response sections derived from a validated result artifact."""

    lead_lines: list[str]
    availability_heading: str | None = None
    availability_lines: list[str] = field(default_factory=list)
    methodology_lines: list[str] = field(default_factory=list)
    # #1468: the "larger districts excluded for missing data" callout for an
    # "N largest by [metric]" ranking. Empty unless that rule fired.
    largest_excluded_lines: list[str] = field(default_factory=list)


def compose_response(plan: QueryPlan, result: ResultSet) -> AnswerComposition:
    """Build deterministic user-facing response sections."""

    if result.result_type == "metric_lookup":
        return _compose_lookup(plan, result)
    if result.result_type == "metric_count":
        return _compose_count(plan, result)
    return _compose_ranking(plan, result)


def _compose_ranking(plan: QueryPlan, result: ResultSet) -> AnswerComposition:
    metric_name = result.rows[0].metric_name if result.rows else "the selected metric"
    sort_metric = sort_metric_name(result)
    frame = result.coverage_frame

    # #1514 D8: stale (prior-year) districts are part of the narrative again —
    # voiced with the canonical "NCTQ last reviewed … ; the value then was …"
    # sentence — and non-answer rows promoted into result.rows (which leave
    # the rendered table) are re-derived as disclosures here so every district
    # without a current answer gets exactly one narrative mention.
    availability_disclosures = [
        *result.coverage_disclosures,
        *promoted_row_disclosures(result),
    ]

    # #1702: partition into reviewed FINDINGS (INA / N-A — NCTQ looked and
    # recorded an outcome with no rankable figure) and data GAPS (everything
    # else). Findings are voiced as their own reviewed-finding sentence in the
    # lead, OUTSIDE the "Not included in ranking" heading and not counted in it
    # (built by reason-count from breakdown fields in _reviewed_finding_line);
    # only the gaps feed the availability block here. Uses the single
    # REVIEWED_NO_FIGURE_STATES authority (shared with execution/ranking.py) so
    # the partition never drifts. NB: this is a strict subset of
    # ANSWER_COVERAGE_STATES — covered-but-non-numeric disclosures
    # (coverage_state="covered") stay on the gap/named path on purpose.
    finding_disclosures = [
        d for d in availability_disclosures if d.coverage_state in REVIEWED_NO_FIGURE_STATES
    ]
    gap_disclosures = [
        d
        for d in availability_disclosures
        if d.coverage_state not in REVIEWED_NO_FIGURE_STATES
    ]

    if availability_disclosures and frame is not None:
        lead = (
            "I found current reviewed numeric data for "
            f"{frame.current_numeric_count} of {frame.in_scope_count} "
            f"{_ranking_coverage_label(plan, result)}. "
            "The ranking below includes only districts with current numeric values."
        )
    elif sort_metric and sort_metric != metric_name:
        lead = (
            f"I ranked {selection_label(plan)} by {sort_metric}, "
            f"{_order_direction(result)}, and displayed {metric_name}."
        )
    else:
        lead = (
            f"I ranked {selection_label(plan)} by {metric_name}, "
            f"{_order_direction(result)}."
        )

    lead_lines = [lead]
    hint_line = _threshold_hint_line(plan, result)
    if hint_line:
        lead_lines.append(hint_line)
    # #1702: append (never prepend — the lead's "I found current reviewed …"
    # opener is contract-pinned) the reviewed-finding sentence so INA/N-A read as
    # findings, not as a coverage exclusion under the availability heading.
    finding_line = _reviewed_finding_line(result, finding_disclosures)
    if finding_line:
        lead_lines.append(finding_line)

    return AnswerComposition(
        lead_lines=lead_lines,
        availability_heading=(
            "Not included in ranking" if gap_disclosures else None
        ),
        availability_lines=_availability_lines(
            plan, result, disclosures_override=gap_disclosures
        ),
        methodology_lines=_methodology_lines(result),
        largest_excluded_lines=_largest_excluded_lines(result),
    )


# #1468: cap on how many excluded-larger districts are named before the answer
# rolls the remainder into "(and X more)".
_LARGEST_EXCLUDED_NAME_CAP = 3


def _largest_excluded_lines(result: ResultSet) -> list[str]:
    """Render the "larger districts excluded for missing data" callout (#1468).

    Two honest buckets, never lumped: reviewed-but-no-figure (``ina``/``na`` —
    a finding) and not-yet-reviewed (a real gap). Each names up to
    ``_LARGEST_EXCLUDED_NAME_CAP`` districts (selection/largest-first order) then
    appends "(and X more)". Empty list when the rule did not fire.
    """

    disclosure = getattr(result, "largest_excluded", None)
    if disclosure is None:
        return []

    lines: list[str] = []
    reviewed_phrase = _excluded_names_phrase(disclosure.reviewed)
    if reviewed_phrase:
        lines.append(
            "Larger districts where NCTQ found the issue not addressed or not "
            f"applicable (a reviewed finding, not a data gap): {reviewed_phrase}."
        )
    gap_phrase = _excluded_names_phrase(disclosure.not_reviewed)
    if gap_phrase:
        lines.append(
            f"Larger districts not yet reviewed for this metric: {gap_phrase}."
        )
    return lines


def _excluded_names_phrase(districts: list[LargestExcludedDistrict]) -> str | None:
    """"A, B, C (and N more)" from excluded-larger entries, or None when empty."""

    if not districts:
        return None
    names = [d.district_name for d in districts]
    shown = names[: _LARGEST_EXCLUDED_NAME_CAP]
    phrase = ", ".join(shown)
    remaining = len(names) - len(shown)
    if remaining > 0:
        phrase = f"{phrase} (and {remaining} more)"
    return phrase


def _filtered_scope_noun(plan: QueryPlan) -> str | None:
    """The singular noun for a size-filtered lookup universe, else None (#942).

    Names how the filter narrowed the population ("small district" /
    "large district") so the denominator sentence can say "Of the N small
    districts …". Only fires for a one-sided enrollment bound; a range or any
    filter we can't confidently name falls through to the generic lookup leads.
    """

    bounds = requested_enrollment_bounds(plan)
    if bounds is None:
        return None
    min_enrollment, max_enrollment = bounds
    if max_enrollment is not None and min_enrollment is None:
        return "small district"
    if min_enrollment is not None and max_enrollment is None:
        return "large district"
    return None


def _list_coverage_sentence(scope_noun: str, summary: ListCoverageSummary) -> str:
    """"Of the N small districts, X (Y%) have current evaluation data." (#942).

    Pluralizes the scope noun and the verb, and shows the share only when it is
    grounded on the result AND the pool is large enough to generalize
    (_MIN_DENOMINATOR_FOR_PERCENT) — the small-sample rule (#744 / FILT-88).
    """

    total = summary.district_total
    with_data = summary.district_with_data
    noun = scope_noun if total == 1 else f"{scope_noun}s"
    verb = "has" if with_data == 1 else "have"
    share = ""
    if summary.percent is not None and total >= _MIN_DENOMINATOR_FOR_PERCENT:
        share = f" ({format_percent(summary.percent)})"
    return f"Of the {total} {noun}, {with_data}{share} {verb} current evaluation data."


def _compose_lookup(plan: QueryPlan, result: ResultSet) -> AnswerComposition:
    # #1613 U6: when the finalizer (U4) answered the resolvable subset of a
    # multi-metric lookup and deferred a metric phrase with no catalog metric,
    # the executor (U5) recorded it on result.selection.unresolved_metrics. Lead
    # with an honest disclosure of the gap, then the normal lookup composition —
    # so the answer never silently drops a requested metric. Orthogonal to which
    # base branch fires (filtered, coverage, criteria, plain), so it wraps them.
    composition = _compose_lookup_base(plan, result)
    unresolved = list(result.selection.unresolved_metrics) if result.selection else []
    if not unresolved:
        return composition
    return replace(
        composition,
        lead_lines=[
            unresolved_metric_sentence(unresolved),
            *composition.lead_lines,
        ],
    )


def unresolved_metric_sentence(unresolved_metrics: list[str]) -> str:
    """Honest disclosure that a requested metric has no catalog metric (#1613 U6).

    Names the unresolvable phrase(s) the finalizer deferred (``deferred_metric_gap``
    → ``ResultSelection.unresolved_metrics``) so a multi-metric lookup answers the
    rest instead of silently dropping it. Contains no numeric tokens (the count of
    answered metrics is not a serialized artifact value, so a digit here would fail
    the numeric-token-provenance guard — cf. the C1 lesson). Ends on the canonical
    "doesn't track ... as a metric" marker so the answer-layer caveat seal protects
    it through the voice pass (the same mechanism that seals "match your criteria").
    """

    phrases = [f"“{phrase}”" for phrase in unresolved_metrics]
    if len(phrases) == 1:
        joined = phrases[0]
    elif len(phrases) == 2:
        joined = f"{phrases[0]} or {phrases[1]}"
    else:
        joined = ", ".join(phrases[:-1]) + f", or {phrases[-1]}"
    return (
        f"Compass doesn't track {joined} as a metric, so the table below covers "
        "the other metrics you asked about."
    )


def _compose_lookup_base(plan: QueryPlan, result: ResultSet) -> AnswerComposition:
    # #1337 / FILT-88: a metric-value-filtered list ("which districts have more
    # than 190 workdays?") leads with how common the match is — matched count +
    # the policy-honest covered denominator + a separate not-reviewed disclosure
    # — using the pre-narrow tally the executor attached. This is the list-path
    # analog of the count path's match-denominator lead; it fires only when the
    # executor set filter_prevalence (single metric-value filter, unambiguous
    # denominator), so unfiltered and multi-filter lookups fall through unchanged.
    filter_prevalence: FilterPrevalenceSummary | None = getattr(
        result, "filter_prevalence", None
    )
    if filter_prevalence is not None:
        # #1651: this path states the denominator ("Of N … match your criteria") AND
        # the cutoff — _filter_prevalence_lead_lines already appends the
        # threshold-hint line, so the reader gets both here. (A single metric-value
        # threshold filter is exactly what sets filter_prevalence.)
        return AnswerComposition(
            lead_lines=_filter_prevalence_lead_lines(plan, result, filter_prevalence),
            methodology_lines=_methodology_lines(result),
        )

    scope_noun = _filtered_scope_noun(plan)
    list_coverage: ListCoverageSummary | None = getattr(result, "list_coverage", None)
    # An intersection/criteria lookup keeps its own "satisfy all requested
    # criteria" lead even when a size filter is present — don't reframe it as a
    # (often tautological) coverage-rate sentence.
    if scope_noun is not None and list_coverage is not None and not result.criteria:
        # Lead with the district-level denominator pattern reviewers require for
        # filtered-universe questions (#942) — names the narrowed universe, the
        # matched count, and the grounded share. coverage_frame counts cells, so
        # the district-level numbers live on result.list_coverage instead.
        lead_lines = [_list_coverage_sentence(scope_noun, list_coverage)]
        if list_coverage.district_total < _MIN_DENOMINATOR_FOR_PERCENT:
            # Spell the threshold — a literal digit here is a numeric token with
            # no artifact provenance and would fail validation (the C1 lesson).
            lead_lines.append(
                "Small sample (fewer than five districts) — I'm not reporting "
                "a percentage."
            )
        hint_line = _threshold_hint_line(plan, result)
        if hint_line:
            lead_lines.append(hint_line)
        return AnswerComposition(
            lead_lines=lead_lines,
            methodology_lines=_methodology_lines(result),
        )

    if result.criteria:
        criterion_labels = [criterion.label for criterion in result.criteria]
        lead = (
            f"I found {len(result.selection.districts) if result.selection else 0} "
            "covered districts that satisfy all requested criteria: "
            f"{'; '.join(criterion_labels)}."
        )
        lead_lines = [lead]
        hint_line = _threshold_hint_line(plan, result)
        if hint_line:
            lead_lines.append(hint_line)
        return AnswerComposition(
            lead_lines=lead_lines,
            methodology_lines=_methodology_lines(result),
        )

    metric_names = sorted({row.metric_name for row in result.rows}, key=str.casefold)
    metric_label = "selected metrics" if len(metric_names) != 1 else metric_names[0]
    # #1514 D9: partial coverage is voiced by counting DISTRICTS, never the
    # retired data-point vocabulary. The tallies are read from the serialized
    # ``district_coverage`` summary (numeric-token provenance) — never
    # re-derived at render time. Out-of-Pathfinder names are voiced separately
    # with the canonical rule-5 sentence (writer), so the lead omits them
    # from Y.
    coverage = result.district_coverage
    if coverage is not None and (
        coverage.districts_not_reviewed or coverage.districts_out_of_universe
    ):
        scope, state_name = _coverage_scope_context(result.selection)
        lead = district_coverage_sentence(coverage, scope=scope, state_name=state_name)
    else:
        lead = f"I looked up {metric_label} for {selection_label(plan)}."

    lead_lines = [lead]
    hint_line = _threshold_hint_line(plan, result)
    if hint_line:
        lead_lines.append(hint_line)

    return AnswerComposition(
        lead_lines=lead_lines,
        methodology_lines=_methodology_lines(result),
    )


def _coverage_scope_phrase(
    scope: ResultSelectionScope, asked_word: str, state_name: str | None
) -> str:
    """The scope-specific noun phrase for the coverage preamble (#1827).

    Names how the population was derived instead of always crediting the user
    with "you asked about": a top-N size cut, a whole state, or the full covered
    universe are *system-derived*, so attributing them to the user misleads (the
    reported "How often do large districts evaluate teachers?" answer said "Of
    the 10 districts you asked about…" for a top-10 the user never listed). One
    place decides the scope noun — the ``DistrictScope``-axis sibling of
    ``_filtered_scope_noun`` (which names the *enrollment-filter* noun on a
    different axis). ``named_districts``/``unspecified`` keep the pre-#1827
    "you asked about" wording.
    """

    if scope == "largest_districts":
        return f"largest {asked_word}"
    if scope == "all_covered_districts":
        return f"{asked_word} Compass covers"
    if scope == "state" and state_name:
        return f"{asked_word} in {state_name}"
    return f"{asked_word} you asked about"


def _coverage_scope_context(
    selection: ResultSelection | None,
) -> tuple[ResultSelectionScope, str | None]:
    """(scope, state_name) for the coverage preamble, read at the result boundary.

    #1827: the scope is read from the *executed* ``ResultSelection`` (which
    execution/selection.py mirrors faithfully from ``plan.selection.scope`` across
    all four scopes), per backend guardrail 7 — anchor phrasing on the result
    boundary, not the planner topology. A missing selection falls back to the
    ``named_districts`` phrasing, so the pre-#1827 sentence is preserved exactly
    when scope is unknown. ``state_name`` is the Title-Case full name(s) for a
    state scope, else None.
    """

    if selection is None:
        return "named_districts", None
    state_name: str | None = None
    if selection.scope == "state" and selection.states:
        names = [
            full for full in (_state_full_name(s) for s in selection.states) if full
        ]
        state_name = _join_phrases(names) if names else None
    return selection.scope, state_name


def district_coverage_sentence(
    coverage: DistrictCoverageSummary,
    *,
    scope: ResultSelectionScope = "named_districts",
    state_name: str | None = None,
) -> str:
    """The canonical district-counting availability sentence (#1514 D9).

    Every number is read from the serialized ``DistrictCoverageSummary``
    (artifacts/results.py — the one derivation authority), so the sentence
    survives the numeric-token-provenance validator.

    #1827: ``scope`` (+ ``state_name`` for a state scope) selects the noun phrase
    so a system-derived population is not narrated as "districts you asked about".
    ``scope`` defaults to ``named_districts`` so existing callers that pass only
    ``coverage`` keep the pre-#1827 "you asked about" wording unchanged.
    """

    asked = coverage.districts_asked
    with_answers = coverage.districts_with_current_data
    not_reviewed = coverage.districts_not_reviewed
    asked_word = "district" if asked == 1 else "districts"
    have_word = "has" if with_answers == 1 else "have"
    scope_phrase = _coverage_scope_phrase(scope, asked_word, state_name)
    sentence = (
        f"Of the {asked} {scope_phrase}, "
        f"{with_answers} {have_word} current reviewed data"
    )
    if not_reviewed:
        havent_word = "hasn't" if not_reviewed == 1 else "haven't"
        year = coverage.requested_academic_year or "the requested year"
        sentence += f"; {not_reviewed} {havent_word} been reviewed for {year} yet"
    return sentence + "."


def unresolved_district_sentences(selection: ResultSelection | None) -> list[str]:
    """Canonical rule-5 sentence per requested name outside the Pathfinder.

    The single composition point for ``selection.unresolved_districts`` →
    "{name}, {ST} is not in the District Policy Pathfinder." — the D6 state
    derivation (execution.selection.split_unresolved_district_state) and the
    sentence authority (artifacts.coverage_label_for_out_of_universe) are
    joined here once; the count composer and the categorical count writer
    both call this instead of restating the loop.
    """

    if selection is None:
        return []
    return [
        coverage_label_for_out_of_universe(display_name, state=state).display
        for display_name, state in (
            split_unresolved_district_state(name, selection=selection)
            for name in selection.unresolved_districts
        )
    ]


def is_single_filter_count(result: ResultSet) -> bool:
    """True for a single-metric count with a real filter.

    Excludes the covered-district *universe* count (filter_statement sentinel,
    no real filter) and multi-metric counts (no single denominator). This is
    exactly the shape that earns the plain match-denominator lead and the
    matching-district-names table, so the composer (the lead) and the writer
    (the names table) stay consistent about which counts list names.
    """

    return (
        len(result.rows) == 1
        and result.rows[0].filter_statement != "covered district universe"
    )


# Below this denominator a share is more misleading than useful (e.g. "1
# (100%)" or "3 (66.7%)" on a tiny pool), so the match-denominator sentence
# states counts only — issue #744 / FILT-88 small-sample rule.
# Single-sourced in rendering/formatting.py so the fact-coverage validator
# shares the same threshold.
_MIN_DENOMINATOR_FOR_PERCENT = MIN_DENOMINATOR_FOR_PERCENT


def _match_the_filter_sentence(
    *, denominator: int, count: int, percent: float | None, pool_phrase: str
) -> str:
    """Shared skeleton for the count-path and list-path denominator leads:
    "Of {denominator} {pool_phrase}, {count} ({pct}%) match your criteria."

    One owner of the verb pluralization, the small-sample percent gate (#744 /
    FILT-88), and the "match your criteria" seal tail, so the two callers cannot
    drift apart. The percent renders only when grounded on the source row/summary
    AND the pool clears the small-sample floor; the single-match case pluralizes
    the verb. The caller owns ``pool_phrase`` (including its own noun
    pluralization) — the only part that differs between the two leads.
    """

    match_word = "matches" if count == 1 else "match"
    share = ""
    if percent is not None and denominator >= _MIN_DENOMINATOR_FOR_PERCENT:
        share = f" ({format_percent(percent)})"
    return f"Of {denominator} {pool_phrase}, {count}{share} {match_word} your criteria."


def match_denominator_sentence(row: CountRow) -> str:
    """The shared "Of N covered districts with data, X (Y%) match your criteria."

    One owner of the sentence reviewers keep asking for (#1415). The percent is
    rendered only when it is grounded on the row (so it survives the numeric-
    token-provenance guard) AND the pool is large enough to be meaningful; the
    single-district case pluralizes correctly and drops the percent.
    """

    district_word = "district" if row.denominator == 1 else "districts"
    return _match_the_filter_sentence(
        denominator=row.denominator,
        count=row.count,
        percent=row.percent,
        pool_phrase=f"covered {district_word} with data",
    )


def filter_prevalence_sentence(
    summary: FilterPrevalenceSummary, *, metric_label: str
) -> str:
    """List-path analog of ``match_denominator_sentence`` (#1337 / FILT-88).

    "Of N districts with a current value for <metric>, X (Y%) match your criteria."
    Every number is read from the serialized ``FilterPrevalenceSummary`` (computed
    pre-narrow), so the sentence survives the numeric-token-provenance guard and
    the denominator is the policy-honest covered count, never the post-narrow
    survivor count. The percent shows only when grounded on the summary AND the
    pool is large enough to generalize (#744 / FILT-88 small-sample rule). Ends
    with "match your criteria" so the answer-layer caveat seal protects it (the same
    marker that seals the count path's lead).
    """

    district_word = "district" if summary.denominator == 1 else "districts"
    return _match_the_filter_sentence(
        denominator=summary.denominator,
        count=summary.matched,
        percent=summary.percent,
        pool_phrase=f"{district_word} with a current value for {metric_label}",
    )


def _filter_prevalence_lead_lines(
    plan: QueryPlan, result: ResultSet, summary: FilterPrevalenceSummary
) -> list[str]:
    """Lead lines for a filtered list answer: the prevalence sentence plus the
    separate not-reviewed / not-applicable disclosures (#1337, #1514 rule 2).

    The disclosures count DISTRICTS and are kept out of the denominator. Numbers
    come from the serialized summary (pre-narrow). Mirrors ``_compose_count``'s
    structure; built as f-strings (not list literals) to stay clear of the
    no-prose-dispatch baseline scanner.

    TODO(#1632): once the answer-first presentation lands, the not-reviewed /
    not-applicable lines move into the folded coverage section below the table;
    they stay inline here until then.
    """

    metric_names = sorted({row.metric_name for row in result.rows}, key=str.casefold)
    metric_label = metric_names[0] if len(metric_names) == 1 else "the requested metric"
    lead_lines = [filter_prevalence_sentence(summary, metric_label=metric_label)]

    year = "the requested year"
    coverage = result.district_coverage
    if coverage is not None and coverage.requested_academic_year:
        year = coverage.requested_academic_year

    # #1514 rule 2: not-reviewed districts disclosed separately, never folded
    # into the denominator. Count is the pre-narrow tally from the summary.
    if summary.not_reviewed_count:
        nr = summary.not_reviewed_count
        district_word = "district" if nr == 1 else "districts"
        # "NCTQ" is the singular subject — always "hasn't" — matching the count
        # path's sentence (_compose_count) so both leads read identically.
        lead_lines.append(
            f"NCTQ hasn't reviewed {nr} {district_word} in this selection "
            f"for {year} yet."
        )

    # Categorical metrics carry a "not applicable" bucket that is excluded from
    # the denominator and disclosed on its own (e.g. parental-leave eligibility).
    if summary.na_count:
        na = summary.na_count
        district_word = "district" if na == 1 else "districts"
        is_word = "is" if na == 1 else "are"
        lead_lines.append(
            f"{na} {district_word} {is_word} marked not applicable for "
            f"{metric_label}."
        )

    hint_line = _threshold_hint_line(plan, result)
    if hint_line:
        lead_lines.append(hint_line)
    return lead_lines


def _compose_count(plan: QueryPlan, result: ResultSet) -> AnswerComposition:
    if result.rows and result.rows[0].filter_statement == "covered district universe":
        lead = "I counted the resolved covered-district selection."
    elif is_single_filter_count(result):
        # Lead with the centralized match-denominator sentence — the count, the
        # denominator, and (now that C3 grounds it on the row) the share are all
        # real artifact values, so the operator-flavored "I counted qualifying
        # covered districts." that dropped them is gone (C1 #1413, C3 #1415).
        lead = match_denominator_sentence(result.rows[0])
    else:
        lead = "I counted qualifying covered districts."
    lead_lines = [lead]
    hint_line = _threshold_hint_line(plan, result)
    if hint_line:
        lead_lines.append(hint_line)
    # #1514 rule 2: districts the count could not see are counted, not
    # silently folded into the denominator note. The number is the serialized
    # DISTRICT tally (district_coverage summary), never the coverage frame's
    # cell count — count frames label one cell per metric, so on multi-metric
    # counts the frame number overstates districts (#1514 review blocker A2).
    coverage = result.district_coverage
    if coverage is not None and coverage.districts_not_reviewed:
        count = coverage.districts_not_reviewed
        district_word = "district" if count == 1 else "districts"
        year = coverage.requested_academic_year or "the requested year"
        lead_lines.append(
            f"NCTQ hasn't reviewed {count} {district_word} in this selection "
            f"for {year} yet."
        )
    # #1514 rule 5: each requested name outside the Pathfinder gets the one
    # canonical sentence, with the D6-derived ", ST" qualifier.
    lead_lines.extend(unresolved_district_sentences(result.selection))
    return AnswerComposition(
        lead_lines=lead_lines,
        methodology_lines=_methodology_lines(result),
    )


def _methodology_lines(result: ResultSet) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for note in result.source_notes:
        if note and note not in seen:
            lines.append(note)
            seen.add(note)

    for ref in result.methodology_codes:
        if ref.audience == "internal":
            continue
        mapped = methodology_line_for(ref)
        if mapped and mapped not in seen:
            lines.append(mapped)
            seen.add(mapped)

    if result.result_type != "metric_ranking" or not result.coverage_disclosures:
        summary = _coverage_summary(result)
        if summary and summary not in seen:
            lines.append(summary)
            seen.add(summary)

    if result.excluded_count and result.result_type != "metric_ranking":
        if result.criteria:
            excluded = (
                "Other rows were filtered out because they did not satisfy every "
                f"requested criterion: {result.excluded_count}."
            )
        else:
            excluded = f"Other rows were filtered out before rendering: {result.excluded_count}."
        if excluded not in seen:
            lines.append(excluded)

    return lines


def methodology_lines_for_result(result: ResultSet) -> list[str]:
    """Return user-facing methodology lines for a validated artifact."""

    return _methodology_lines(result)



def _coverage_summary(result: ResultSet) -> str:
    """The "Data availability:" line, in district vocabulary (#1514 D9).

    Reads the serialized ``district_coverage`` summary — the one artifact-
    layer derivation (artifacts/results.py) — for every result shape; a
    result whose summary cannot be derived honestly carries ``None`` and
    gets no line. "Data point" vocabulary is retired.
    """

    frame = result.coverage_frame
    if frame is None:
        return ""
    # Suppress the data-availability line when data coverage is high — the lead
    # paragraph already conveys the coverage counts.
    if frame.in_scope_count and (
        frame.current_numeric_count / frame.in_scope_count >= _COVERAGE_VERBOSE_THRESHOLD
    ):
        log_info(
            "coverage_rendered_decision",
            coverage_ratio=frame.current_numeric_count / frame.in_scope_count,
            rendered=False,
            threshold=_COVERAGE_VERBOSE_THRESHOLD,
        )
        return ""
    coverage_ratio = (
        frame.current_numeric_count / frame.in_scope_count if frame.in_scope_count else 0.0
    )
    log_info(
        "coverage_rendered_decision",
        coverage_ratio=coverage_ratio,
        rendered=True,
        threshold=_COVERAGE_VERBOSE_THRESHOLD,
    )
    coverage = result.district_coverage
    if coverage is None or not coverage.districts_asked:
        return ""
    scope, state_name = _coverage_scope_context(result.selection)
    summary = "Data availability: " + district_coverage_sentence(
        coverage, scope=scope, state_name=state_name
    )
    if coverage.districts_out_of_universe:
        out_of_universe = coverage.districts_out_of_universe
        was_word = "was" if out_of_universe == 1 else "were"
        name_word = _pluralize("name", out_of_universe)
        summary += (
            f" {out_of_universe} requested {name_word} {was_word} "
            "outside the District Policy Pathfinder."
        )
    return summary


# #1702: coverage reasons whose excluded districts are voiced by COUNTING (never
# named per-district). Each maps to its single serialized
# ``CoverageBreakdown.{reason}_count`` field — that field is the one authority
# for the number, and equals the count of gap disclosures carrying that reason
# (``coverage_breakdown_from_labels`` builds one count per label reason, and
# every not-reviewed cell appears exactly once across coverage_disclosures +
# promoted rows). NB: ``unavailable`` rolls up under ``metric_not_reviewed`` in
# practice today, but kept as its own clause for the post-#1698 ``Unavailable``
# value (criterion 26's "inventive phrasing" complaint).
_COUNTED_GAP_REASONS: tuple[str, ...] = (
    "unavailable",
    "metric_not_reviewed",
    "district_not_reviewed",
)

# #1702: coverage reasons voiced by NAMING the district per-district when the
# detail gate is open (≤5, or a named-district scope), and by a counted fallback
# when there are too many to name. Their canonical per-district sentences are
# already built upstream on ``disclosure.display``.
_NAMED_GAP_REASONS: tuple[str, ...] = (
    "stale_recent_answer",
    "out_of_universe",
    "non_numeric_rank_exclusion",
)

# #1702: reason -> (count clause for the GAP block). ``{count}`` and
# ``{districts}`` are filled from a single serialized breakdown field. The clause
# stands alone (no mixed grand-total intro — KTD3), so each carries its own
# "NCTQ hasn't reviewed …" / "The reviewed value is unavailable …" framing.
_GAP_COUNT_CLAUSE: dict[str, str] = {
    "unavailable": "The reviewed value is unavailable for {count} {districts}.",
    "metric_not_reviewed": (
        "NCTQ hasn't reviewed {count} {districts} for {year} yet."
    ),
    "district_not_reviewed": (
        "NCTQ hasn't reviewed {count} {districts} for {year} yet."
    ),
    "stale_recent_answer": (
        "NCTQ last reviewed {count} {districts} in an earlier year."
    ),
    "out_of_universe": (
        "{count} {districts} {verb_be} outside the District Policy Pathfinder."
    ),
    "non_numeric_rank_exclusion": (
        "{count} {districts} {verb_have} a current reviewed non-numeric value."
    ),
}

# #1702: reason -> the serialized ``CoverageBreakdown`` field that backs its
# count token. One authority for "which field is this reason's count".
_BREAKDOWN_COUNT_FIELD: dict[str, str] = {
    "unavailable": "unavailable_count",
    "metric_not_reviewed": "metric_not_reviewed_count",
    "district_not_reviewed": "district_not_reviewed_count",
    "stale_recent_answer": "stale_recent_answer_count",
    "out_of_universe": "out_of_universe_count",
    "non_numeric_rank_exclusion": "non_numeric_rank_exclusion_count",
    "issue_not_addressed": "issue_not_addressed_count",
    "not_applicable": "not_applicable_count",
}


def _breakdown_count(result: ResultSet, reason: str) -> int:
    """The serialized breakdown count backing ``reason``'s narration token (#1702).

    Reading ``CoverageBreakdown.{reason}_count`` keeps every count token provenance
    -safe: the value is a single serialized field present in
    ``result.model_dump_json()``, never a renderer-side ``len()`` or sum.
    """

    frame = getattr(result, "coverage_frame", None)
    if frame is None:
        return 0
    return int(getattr(frame.breakdown, _BREAKDOWN_COUNT_FIELD[reason], 0))


def _reason_count_lines(result: ResultSet, reasons: tuple[str, ...]) -> list[str]:
    """One counted clause per ``reason`` present (#1702, KTD4).

    The single-owner coverage-counting narrator: each clause's number is a single
    serialized ``CoverageBreakdown.{reason}_count`` field (``_breakdown_count``),
    mirroring ``writer.py::_categorical_coverage_sentence``. Reasons with a zero
    count emit nothing.
    """

    year = (
        result.district_coverage.requested_academic_year
        if result.district_coverage is not None
        else None
    ) or "the requested year"
    lines: list[str] = []
    for reason in reasons:
        count = _breakdown_count(result, reason)
        if count <= 0:
            continue
        lines.append(
            _GAP_COUNT_CLAUSE[reason].format(
                count=count,
                districts=_pluralize("district", count),
                verb_be="is" if count == 1 else "are",
                verb_have="has" if count == 1 else "have",
                year=year,
            )
        )
    return lines


def _reviewed_finding_line(
    result: ResultSet,
    finding_disclosures: list[CoverageDisclosure],
) -> str | None:
    """The reviewed-finding sentence for excluded INA / N-A districts (#1702, R3).

    INA ("issue not addressed") and N-A ("not applicable") are reviewed FINDINGS,
    not data gaps: NCTQ looked and recorded an outcome with no rankable figure.
    When such a district is EXCLUDED from the ranking (it carries a disclosure,
    not a table row), it is voiced here as its own sentence — placed in the lead,
    OUTSIDE the "Not included in ranking" heading and never counted in it.

    Gating vs. counting (the same split the retired intro documented): the
    sentence is gated on ``finding_disclosures`` (presence of an EXCLUDED finding),
    but each count token is a single serialized ``CoverageBreakdown`` field
    (``issue_not_addressed_count`` / ``not_applicable_count``) so no token is a
    renderer-side ``len()`` or sum. Those two coincide because only the
    metric-ranking builder emits INA/N-A as disclosures, and there every
    non-covered cell becomes exactly one disclosure-and-label and never a row, so
    ``len(INA disclosures) == breakdown.issue_not_addressed_count`` (same for
    N-A). The profile-ordered builder keeps INA/N-A as TABLE ROWS and emits no
    such disclosures — so ``finding_disclosures`` is empty there and this sentence
    is suppressed, leaving the in-table cell as the only mention (no
    double-narration). INA and N-A are SEPARATE clauses. Returns None when no
    excluded finding is present.
    """

    has_ina = any(d.coverage_state == "ina" for d in finding_disclosures)
    has_na = any(d.coverage_state == "na" for d in finding_disclosures)
    clauses: list[str] = []
    if has_ina:
        ina = _breakdown_count(result, "issue_not_addressed")
        clauses.append(
            f"reviewed {ina} more {_pluralize('district', ina)} where the issue "
            "is not addressed"
        )
    if has_na:
        # Second clause shares the leading "NCTQ reviewed/found" verb; phrase it
        # so it reads on its own ("… and found N where the policy is not
        # applicable") without re-counting a combined total.
        na = _breakdown_count(result, "not_applicable")
        verb = "found" if clauses else "reviewed"
        clauses.append(
            f"{verb} {na} {_pluralize('district', na)} where the policy is not "
            "applicable"
        )
    if not clauses:
        return None
    return "NCTQ " + _join_phrases(clauses) + "."


def _availability_lines(
    plan: QueryPlan,
    result: ResultSet,
    *,
    disclosures_override: list[CoverageDisclosure] | None = None,
) -> list[str]:
    """The "Not included in ranking" data-GAP block (#1702).

    INA / N-A findings are partitioned out upstream (``_compose_ranking``) and
    voiced separately, so this block only narrates real gaps:

    - COUNTED reasons (unavailable / metric_/district_not_reviewed) → always one
      counted clause each (``_reason_count_lines``), gate-independent.
    - NAMED reasons (stale / out_of_universe / non_numeric-covered) → per-district
      canonical sentences when the detail gate is open (≤5 or named-district
      scope), else a counted fallback (too many to name).

    The mixed grand-total intro is dropped (KTD3): each clause carries its own
    field-backed count, so no renderer-side subtraction is needed.
    """

    disclosures = (
        disclosures_override
        if disclosures_override is not None
        else result.coverage_disclosures
    )
    if not disclosures:
        return []

    counted_lines = _reason_count_lines(result, _COUNTED_GAP_REASONS)

    # Allowlist (not denylist): only the explicitly NAMED reasons get the
    # per-district sentence. Any other gap reason — including a not_reviewed
    # disclosure with an UNMAPPED/None reason (reachable via the promoted-row
    # path) — falls to the COUNTED clauses above, never to the formatter's
    # "{district}: Not reviewed" label that criterion 26 forbids. (A denylist on
    # _COUNTED_GAP_REASONS let bare "not_reviewed" leak to the label path.)
    named_disclosures = [
        d for d in disclosures if (d.reason or d.coverage_state) in _NAMED_GAP_REASONS
    ]
    if _should_show_detailed_coverage_disclosures(disclosures, result):
        named_lines = [
            _format_coverage_disclosure(disclosure) for disclosure in named_disclosures
        ]
    else:
        named_lines = _reason_count_lines(result, _NAMED_GAP_REASONS)

    return [*counted_lines, *named_lines]


def _should_show_detailed_coverage_disclosures(
    disclosures: list[CoverageDisclosure],
    result: ResultSet,
) -> bool:
    if len(disclosures) <= _DETAILED_COVERAGE_DISCLOSURE_LIMIT:
        return True
    if result.selection and result.selection.scope == "named_districts":
        return True
    return False


def _format_coverage_disclosure(disclosure: CoverageDisclosure) -> str:
    """Per-district NAMED-reason sentence (#1702).

    Reached only for the NAMED gap reasons (stale / out_of_universe /
    covered-non-numeric) now that INA/N-A are partitioned out upstream and
    not_reviewed/unavailable are COUNTED. Its sole caller is ``_availability_lines``;
    the lookup/Citation path (#1686) does not call it.
    """

    if disclosure.reason == "non_numeric_rank_exclusion":
        detail = (
            "Current reviewed value is non-numeric and was not ranked: "
            f"{disclosure.display}."
        )
    elif disclosure.coverage_state == "covered":
        detail = f"Current reviewed value is non-numeric: {disclosure.display}."
    elif (
        disclosure.reason == "stale_recent_answer"
        or disclosure.coverage_state == "out_of_universe"
    ):
        # #1514 D7/D6: these states are voiced ONLY with their canonical sentences
        # ("NCTQ last reviewed … ; the value then was …" / "… is not in the
        # District Policy Pathfinder.") — already built upstream on
        # disclosure.display and naming the district in prose.
        return disclosure.display
    else:
        detail = short_coverage_label(disclosure.reason, disclosure.display)
    return f"{disclosure.district_name}: {detail}"


def _ranking_coverage_label(plan: QueryPlan, result: ResultSet) -> str:
    if result.selection and result.selection.scope == "named_districts":
        return "requested districts"
    states = _result_or_plan_states(plan, result)
    if states:
        return f"{', '.join(states)} districts NCTQ tracks"
    return "districts NCTQ tracks"


def _result_or_plan_states(plan: QueryPlan, result: ResultSet) -> list[str]:
    if result.selection and result.selection.states:
        return sorted(_normalize_state(state) for state in result.selection.states)
    return sorted(_requested_states(plan))


def _pluralize(value: str, count: int) -> str:
    return value if count == 1 else f"{value}s"


def _join_phrases(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    if len(parts) == 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _order_direction(result: ResultSet) -> str:
    statement = result.order_statement.casefold()
    if "lowest to highest" in statement:
        return "lowest to highest"
    return "highest to lowest"
