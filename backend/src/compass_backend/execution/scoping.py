"""DeterministicQueryExecutor scoping + metric-resolution methods (mixin).

Extracted from `executor.py` to split the 1,852-LOC file along its natural
internal section boundary. The methods access executor instance state
(`self._catalog`, `self._repository`, `self._current_academic_year`)
and free helpers from `._helpers`.

This is a mixin (not a standalone class) — the methods only make sense
when mixed into `DeterministicQueryExecutor`. Behavior is unchanged
from the pre-split version.

Note: the existing `execution/selection.py` already owns `resolve_selection`
and `requested_states` free functions; this file (`scoping.py`) holds the
executor-instance scoping methods, which are a different concern.
"""

from __future__ import annotations

from compass_backend.artifacts import ResultSelection, SelectedDistrict
from compass_backend.catalog import (
    ContextualMetricDefault,
    MetricBundleResolution,
    MetricCandidate,
)
from compass_backend.contracts.planning import MetricSpec, QueryPlan

from compass_backend.answer_layer.clarify import compose_clarify_question_async

from ._helpers import (
    _execution_metric_specs,
    _metric_resolution_report,
)
from .count import categorical_count_metric_queries
from .types import (
    ExecutionClarification,
    ExecutionOutcome,
    ExecutionRefusal,
)


def _distinct_candidate_count(candidates: list[MetricCandidate]) -> int:
    """Count distinct catalog metrics in a candidate list (dedup by metric_id)."""

    return len({candidate.metric_id for candidate in candidates})


def _best_guess_metric(
    candidates: list[MetricCandidate],
) -> tuple[MetricCandidate, list[MetricCandidate]] | None:
    """Fix 4B: split a score-ranked candidate list into (primary, alternates).

    Candidates arrive score-ranked from ``resolve_metric_bundle``, so
    ``candidates[0]`` is the top match — promote it, and return the remaining
    distinct candidates (deduped by ``metric_id``) as the alternates the caller
    discloses. Returns ``None`` when there is nothing to promote.
    """

    if not candidates:
        return None
    primary = candidates[0]
    alternates: list[MetricCandidate] = []
    seen = {primary.metric_id}
    for candidate in candidates[1:]:
        if candidate.metric_id in seen:
            continue
        seen.add(candidate.metric_id)
        alternates.append(candidate)
    return primary, alternates


class _ScopingMixin:
    """Provides district/metric scoping methods to DeterministicQueryExecutor."""

    async def _scope_districts(self, selection: ResultSelection) -> list[SelectedDistrict]:
        if selection.districts:
            return selection.districts
        districts = await self._catalog.list_covered_districts(
            states=set(selection.states) or None,
        )
        return [
            SelectedDistrict(
                district_id=district.district_id,
                district_name=district.district_name,
                state=district.state,
            )
            for district in districts
        ]

    async def _materialize_scope_selection(
        self,
        selection: ResultSelection,
    ) -> ResultSelection:
        if selection.scope not in {"all_covered_districts", "state"}:
            return selection
        scope_districts = await self._scope_districts(selection)
        return selection.model_copy(update={"districts": scope_districts})

    async def _reviewed_district_ids(
        self,
        districts: list[SelectedDistrict],
        *,
        academic_year: str | None = None,
    ) -> set[int]:
        district_ids = {district.district_id for district in districts}
        return await self._repository.fetch_reviewed_district_ids(
            academic_year=academic_year or self._current_academic_year,
            district_ids=district_ids,
        )

    async def _fetch_metric_rows_for_year(
        self,
        *,
        metric_id: int,
        academic_year: str,
        district_ids: set[int],
    ):
        scoped_fetch = getattr(self._repository, "fetch_metric_answer_rows_for_year", None)
        if scoped_fetch is not None and district_ids:
            return await scoped_fetch(
                metric_id=metric_id,
                academic_year=academic_year,
                district_ids=district_ids,
            )
        return await self._repository.fetch_metric_answer_rows(
            metric_id=metric_id,
            academic_year=academic_year,
        )

    async def _metric_clarification_from_question(
        self,
        plan: QueryPlan,
        *,
        numeric_only: bool,
    ) -> ExecutionOutcome | None:
        # #1008: the executor no longer re-reads plan.question to decide
        # whether a broad observation phrasing needs a metric-family
        # clarification — that prose inspection was a guardrail #4 violation
        # ("no prose dispatch below the planning boundary"). The catalog now
        # surfaces the ambiguity as a *typed* MetricBundleResolution, and this
        # method consumes only that typed result. The typed suppressor below
        # reads plan.metrics/plan.filters, never prose. The deeper zero-prose
        # end-state (a typed planner "concept_mentions" surface) is #1248
        # planning-redesign follow-up.
        if _plan_names_specific_observation_lane(plan):
            return None
        metric_resolution = await self._catalog.clarifying_observation_metric_bundle(
            plan.question,
            numeric_only=numeric_only,
            limit=5,
        )
        if metric_resolution is None:
            return None
        clarification = await compose_clarify_question_async(
            plan.question,
            operation=plan.operation,
            candidates=metric_resolution.candidates,
            adjudicator_hint=metric_resolution.clarification_hint,
        )
        return ExecutionClarification(
            clarification=clarification,
            message=clarification.question,
            resolution_report=_metric_resolution_report(
                plan,
                plan.question,
                metric_resolution,
                entity_type="metric_bundle",
            ),
        )

    async def _contextual_metric_default(
        self,
        metric_spec: MetricSpec,
        *,
        numeric_only: bool,
    ) -> ContextualMetricDefault | None:
        """Commit the governed salary default instead of clarifying (#1248 WS-1).

        Mirrors the rank path
        (``operations._resolve_rank_primary_metric_with_default``): a broad
        salary bundle is ambiguous only because the degree lane is unstated.
        The planner stays ``null`` (``planner.md`` — "'starting salary' alone →
        null … do not silently default"); the *catalog* applies the governed
        ``launch_starting_salary_default`` (→ metric 89, bachelor's starting
        salary) plus its disclosure note. Returns ``None`` for any spec the
        planner already lane-resolved, or whose alias does not carry that
        governed context key (bare 'teacher salary' max-BA bundle, school-days,
        observation, district ambiguity) — so those still clarify.
        """

        if metric_spec.degree_lane is not None:
            return None
        return await self._catalog.resolve_contextual_metric_default(
            metric_spec.name,
            context_key="launch_starting_salary_default",
            numeric_only=numeric_only,
        )

    async def _metric_clarification_or_refusal(
        self,
        plan: QueryPlan,
        phrase: str,
        metric_resolution: MetricBundleResolution,
        *,
        refusal_message: str,
    ) -> ExecutionOutcome:
        """Clarify with the real candidates, or refuse when there are none.

        #1248 SELECT-R4: a metric phrase that surfaces *multiple* real candidates
        but does not resolve cleanly is recoverable — list the candidates so the
        user can disambiguate, mirroring the existing ``ambiguous`` branch — not
        a generic dead-end. A phrase with zero (or a single, e.g. numeric-only
        filtered) candidate is genuinely unrecoverable here, so it keeps the
        deterministic refusal. ``metric_resolution.candidates`` carries the
        recoverable signal that the historic dead-ends discarded.
        """

        report = _metric_resolution_report(
            plan,
            phrase,
            metric_resolution,
            entity_type="metric_bundle",
        )
        if _distinct_candidate_count(metric_resolution.candidates) > 1:
            clarification = await compose_clarify_question_async(
                phrase,
                operation=plan.operation,
                candidates=metric_resolution.candidates,
                adjudicator_hint=metric_resolution.clarification_hint,
            )
            return ExecutionClarification(
                clarification=clarification,
                message=clarification.question,
                resolution_report=report,
                ambiguous_metric_phrase=phrase,
            )
        return ExecutionRefusal(
            message=refusal_message,
            resolution_report=report,
        )

    async def _resolve_plan_metrics(
        self,
        plan: QueryPlan,
        *,
        numeric_only: bool,
    ) -> tuple[list[MetricCandidate], list[str], list[MetricCandidate]] | ExecutionOutcome:
        """Resolve a plan's metric specs into candidates plus disclosure notes.

        Returns ``(metrics, source_notes, alternate_candidates)`` on success.
        ``source_notes`` carries any governed default-commit disclosures
        (#1248 WS-1) so the caller can thread them onto the result's
        ``source_notes``; it is empty for cleanly resolved specs.
        ``alternate_candidates`` carries the not-chosen candidates when a
        materially-ambiguous metric phrase was best-guessed (Fix 4B) so the
        caller can DISCLOSE them; empty for cleanly-resolved specs.
        """

        question_clarification = await self._metric_clarification_from_question(
            plan,
            numeric_only=numeric_only,
        )
        if question_clarification is not None:
            return question_clarification

        metrics: list[MetricCandidate] = []
        seen_metric_ids: set[int] = set()
        source_notes: list[str] = []
        alternate_candidates: list[MetricCandidate] = []
        seen_alternate_ids: set[int] = set()
        for metric_spec in _execution_metric_specs(plan):
            metric_resolution = await self._catalog.resolve_metric_bundle(
                metric_spec.name,
                numeric_only=numeric_only,
                limit=5,
                degree_lane=metric_spec.degree_lane,
            )
            if metric_resolution.ambiguous:
                contextual_default = await self._contextual_metric_default(
                    metric_spec,
                    numeric_only=numeric_only,
                )
                if contextual_default is not None:
                    if contextual_default.metric.metric_id not in seen_metric_ids:
                        metrics.append(contextual_default.metric)
                        seen_metric_ids.add(contextual_default.metric.metric_id)
                    source_notes.extend(
                        note.note_text for note in contextual_default.renderer_notes
                    )
                    continue
                # Fix 4B (#1 refusal family): a materially-ambiguous metric tie
                # with NO governed contextual default ("paid sick days in the
                # first year" → 5 leave metrics at 0.6) is answerable — the same
                # product decision the planning-time gate applies
                # (_apply_adjudication). Best-guess the top-ranked candidate and
                # CARRY the rest as alternates for the caller to disclose, instead
                # of clarifying to a dead-end / canned rescue. TYPED fields only
                # (candidate metric_id/name) — no prose dispatch. This changes
                # residual metric clarify → best-guess for peer/count/single-metric
                # lookups; it is eval-gated (scorecard).
                best_guess = _best_guess_metric(metric_resolution.candidates)
                if best_guess is not None:
                    primary, alternates = best_guess
                    if primary.metric_id not in seen_metric_ids:
                        metrics.append(primary)
                        seen_metric_ids.add(primary.metric_id)
                    for alt in alternates:
                        if (
                            alt.metric_id != primary.metric_id
                            and alt.metric_id not in seen_alternate_ids
                        ):
                            alternate_candidates.append(alt)
                            seen_alternate_ids.add(alt.metric_id)
                    continue
                # Defensive: an ambiguous resolution with no usable candidate to
                # promote still clarifies (should not occur — ambiguous implies
                # candidates exist).
                clarification = await compose_clarify_question_async(
                    metric_spec.name,
                    operation=plan.operation,
                    candidates=metric_resolution.candidates,
                    adjudicator_hint=metric_resolution.clarification_hint,
                )
                return ExecutionClarification(
                    clarification=clarification,
                    message=clarification.question,
                    ambiguous_metric_phrase=metric_spec.name,
                    resolution_report=_metric_resolution_report(
                        plan,
                        metric_spec.name,
                        metric_resolution,
                        entity_type="metric_bundle",
                    ),
                )
            if not metric_resolution.resolved:
                return await self._metric_clarification_or_refusal(
                    plan,
                    metric_spec.name,
                    metric_resolution,
                    refusal_message=(
                        "I could not resolve every requested metric for deterministic "
                        f"execution yet: {metric_spec.name}."
                    ),
                )
            for metric in metric_resolution.resolved:
                if metric.metric_id in seen_metric_ids:
                    continue
                metrics.append(metric)
                seen_metric_ids.add(metric.metric_id)
            # Fix 4B: also carry the adjudicator's adjacent variants
            # (``select_with_alternates``) so a confident primary pick over a
            # materially-ambiguous phrase discloses its alternates too — the same
            # "never silently drop alternates" contract as the best-guess branch.
            for alt in metric_resolution.alternate_candidates:
                if (
                    alt.metric_id not in seen_metric_ids
                    and alt.metric_id not in seen_alternate_ids
                ):
                    alternate_candidates.append(alt)
                    seen_alternate_ids.add(alt.metric_id)
        return metrics, source_notes, alternate_candidates

    async def _resolve_plan_metric_groups(
        self,
        plan: QueryPlan,
        *,
        numeric_only: bool,
    ) -> (
        tuple[
            list[tuple[str, str, list[MetricCandidate]]],
            list[MetricCandidate],
            list[str],
        ]
        | ExecutionOutcome
    ):
        """Resolve metric specs into labeled groups for the lookup operation.

        Returns a 3-tuple of (groups, alternate_candidates, source_notes) on
        success, or an ExecutionOutcome (clarification/refusal) on failure.
        ``alternate_candidates`` is the union of
        ``MetricBundleResolution.alternate_candidates`` across all resolved specs
        — populated only when the adjudicator emits
        ``action='select_with_alternates'``. ``source_notes`` carries any
        governed default-commit disclosures (#1248 WS-1) for the caller to thread
        onto the result.
        """

        question_clarification = await self._metric_clarification_from_question(
            plan,
            numeric_only=numeric_only,
        )
        if question_clarification is not None:
            return question_clarification

        groups: list[tuple[str, str, list[MetricCandidate]]] = []
        alternate_candidates: list[MetricCandidate] = []
        seen_alternate_ids: set[int] = set()
        source_notes: list[str] = []
        for index, metric_spec in enumerate(_execution_metric_specs(plan), start=1):
            metric_resolution = await self._catalog.resolve_metric_bundle(
                metric_spec.name,
                numeric_only=numeric_only,
                limit=5,
                degree_lane=metric_spec.degree_lane,
            )
            if metric_resolution.ambiguous:
                contextual_default = await self._contextual_metric_default(
                    metric_spec,
                    numeric_only=numeric_only,
                )
                if contextual_default is not None:
                    groups.append(
                        (
                            f"criterion_{index}",
                            metric_spec.name,
                            [contextual_default.metric],
                        )
                    )
                    source_notes.extend(
                        note.note_text for note in contextual_default.renderer_notes
                    )
                    continue
                clarification = await compose_clarify_question_async(
                    metric_spec.name,
                    operation=plan.operation,
                    candidates=metric_resolution.candidates,
                    adjudicator_hint=metric_resolution.clarification_hint,
                )
                return ExecutionClarification(
                    clarification=clarification,
                    message=clarification.question,
                    ambiguous_metric_phrase=metric_spec.name,
                    resolution_report=_metric_resolution_report(
                        plan,
                        metric_spec.name,
                        metric_resolution,
                        entity_type="metric_bundle",
                    ),
                )
            if not metric_resolution.resolved:
                return await self._metric_clarification_or_refusal(
                    plan,
                    metric_spec.name,
                    metric_resolution,
                    refusal_message=(
                        "I could not resolve every requested metric for deterministic "
                        f"execution yet: {metric_spec.name}."
                    ),
                )

            group_metrics: list[MetricCandidate] = []
            seen_metric_ids: set[int] = set()
            for metric in metric_resolution.resolved:
                if metric.metric_id in seen_metric_ids:
                    continue
                group_metrics.append(metric)
                seen_metric_ids.add(metric.metric_id)
            groups.append((f"criterion_{index}", metric_spec.name, group_metrics))

            for alt in metric_resolution.alternate_candidates:
                if alt.metric_id not in seen_alternate_ids:
                    alternate_candidates.append(alt)
                    seen_alternate_ids.add(alt.metric_id)

        return groups, alternate_candidates, source_notes

    async def _resolve_categorical_count_metrics(
        self,
        plan: QueryPlan,
    ) -> list[MetricCandidate] | ExecutionOutcome:
        metrics: list[MetricCandidate] = []
        seen_metric_ids: set[int] = set()
        # #1248 SELECT-R4: remember the unresolved query whose resolution carried
        # the richest candidate set so we can clarify with real candidates after
        # the loop instead of dead-ending. The historic refusal below the loop
        # discarded every candidate the catalog surfaced.
        best_unresolved_query: str | None = None
        best_unresolved: MetricBundleResolution | None = None
        for query in categorical_count_metric_queries(plan):
            metric_resolution = await self._catalog.resolve_metric_bundle(
                query,
                numeric_only=False,
                limit=5,
            )
            if metric_resolution.ambiguous:
                clarification = await compose_clarify_question_async(
                    query,
                    operation=plan.operation,
                    candidates=metric_resolution.candidates,
                    adjudicator_hint=metric_resolution.clarification_hint,
                )
                return ExecutionClarification(
                    clarification=clarification,
                    message=clarification.question,
                    resolution_report=_metric_resolution_report(
                        plan,
                        query,
                        metric_resolution,
                        entity_type="metric_bundle",
                    ),
                )
            if not metric_resolution.resolved:
                if best_unresolved is None or _distinct_candidate_count(
                    metric_resolution.candidates
                ) > _distinct_candidate_count(best_unresolved.candidates):
                    best_unresolved_query = query
                    best_unresolved = metric_resolution
                continue
            for metric in metric_resolution.resolved:
                if metric.metric_id in seen_metric_ids:
                    continue
                metrics.append(metric)
                seen_metric_ids.add(metric.metric_id)
            if metrics:
                return metrics

        refusal_message = (
            "I could not resolve an approved categorical field for "
            "deterministic count execution yet."
        )
        if best_unresolved is not None:
            return await self._metric_clarification_or_refusal(
                plan,
                best_unresolved_query or "",
                best_unresolved,
                refusal_message=refusal_message,
            )
        return ExecutionRefusal(message=refusal_message)

    async def _resolve_topic_coverage_metrics(
        self,
        plan: QueryPlan,
    ) -> tuple[list[MetricCandidate], str] | ExecutionOutcome:
        """Resolve a topic-coverage count's topic phrase to the topic's metrics.

        The planner emits the topic as a single ``MetricSpec(name=<topic>)``;
        this searches governed topics for that phrase and returns every metric
        linked to the best-matching topic plus the topic's display name for the
        answer label. A topic that matches nothing governed refuses rather than
        guessing a metric.
        """

        phrase = plan.metrics[0].name if plan.metrics else ""
        # search_topics matches a topic name as a SUBSTRING of the query, so a
        # phrase longer than the topic ("Evaluation policies" vs the
        # "Evaluation" topic) misses. Try the phrase, then narrower variants
        # (generic words dropped, then each significant word) until the topic
        # name appears as the whole query.
        topics: list = []
        for query in _topic_search_variants(phrase):
            topics = await self._catalog.search_topics(query, limit=3)
            if topics:
                break
        if not topics:
            return ExecutionRefusal(
                message=(
                    "I couldn't match that to a Compass policy topic. Try a "
                    "topic like evaluation, salary, benefits, leave, or "
                    "collective bargaining."
                ),
            )
        topic = topics[0]
        metrics = await self._catalog.fetch_topic_metric_candidates(
            [topic], limit=60
        )
        if not metrics:
            return ExecutionRefusal(
                message=(
                    f"I don't have governed metrics linked to the "
                    f"{topic.topic_name} topic for a coverage count yet."
                ),
            )
        return metrics, topic.topic_name


_TOPIC_GENERIC_WORDS = frozenset(
    {
        "policies",
        "policy",
        "data",
        "information",
        "info",
        "coverage",
        "addressing",
        "about",
        "on",
        "for",
        "the",
        "their",
        "any",
    }
)


def _topic_search_variants(phrase: str) -> list[str]:
    """Return topic-search queries to try, broad to narrow.

    ``search_topics`` matches a governed topic name as a SUBSTRING of the query,
    so a phrase longer than the topic ("evaluation policies" vs the "Evaluation"
    topic) never matches as-is. Yield the full phrase, then the phrase with
    generic words ("policies", "data", …) removed, then each remaining
    significant word longest-first — so the topic name eventually equals the
    whole query.
    """

    phrase = (phrase or "").strip()
    variants: list[str] = []
    seen: set[str] = set()

    def _add(query: str) -> None:
        query = query.strip()
        key = query.casefold()
        if query and key not in seen:
            seen.add(key)
            variants.append(query)

    _add(phrase)
    words = [word for word in phrase.split() if word]
    significant = [
        word for word in words if word.casefold() not in _TOPIC_GENERIC_WORDS
    ]
    _add(" ".join(significant))
    for word in sorted(significant, key=len, reverse=True):
        _add(word)
    return variants


def _plan_names_specific_observation_lane(plan: QueryPlan) -> bool:
    """Return whether the planner already picked an observation metric lane."""

    if not plan.filters:
        return False
    for metric in _execution_metric_specs(plan):
        name = metric.name.casefold()
        if (
            "observation" in name
            and ("formal" in name or "informal" in name)
            and ("non-tenured" in name or "non tenured" in name or "tenured" in name)
        ):
            return True
    return False
