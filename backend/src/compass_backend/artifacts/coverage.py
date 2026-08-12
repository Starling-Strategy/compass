"""Coverage-state artifacts and canonical display rules."""

from __future__ import annotations

from typing import Any, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

CoverageState = Literal["covered", "ina", "na", "not_reviewed", "out_of_universe"]
CoverageReason = Literal[
    "answer_value",
    "issue_not_addressed",
    "not_applicable",
    "metric_not_reviewed",
    "district_not_reviewed",
    "stale_recent_answer",
    "non_numeric_rank_exclusion",
    "out_of_universe",
    "profile_field_value",
    "covered_universe_count",
    "count_summary",
    "categorical_value_count",
    "unavailable",
]

_INA_DISPLAY = "Issue not addressed in the documents reviewed."
SPARSE_COVERAGE_MIN_IN_SCOPE_CELLS = 3
SPARSE_COVERAGE_RATIO_THRESHOLD = 0.5

# #1514 — the single answer-state predicate. Tables, charts, and CSV data rows
# hold answer rows only (a value, INA, or N/A); not_reviewed and
# out_of_universe rows are voiced as narrative sentences that count districts,
# never data points. Writer, CSV builders, and parity validators all import
# this — never restate the filter locally.
ANSWER_COVERAGE_STATES: frozenset[str] = frozenset({"covered", "ina", "na"})

# #1702 — the reviewed-but-no-rankable-figure states. INA ("issue not
# addressed") and N/A ("not applicable") are reviewed FINDINGS, not data gaps:
# NCTQ looked and recorded an outcome that simply has no numeric figure to rank.
# The ranking availability narrator partitions disclosures on this set so those
# findings are voiced as their own reviewed-finding sentence — never lumped with
# (or counted under) the "Not included in ranking" data-gap block. This is the
# subset of ANSWER_COVERAGE_STATES that excludes "covered" (covered-but-
# non-numeric disclosures stay on the gap/named path), so it is its own constant
# rather than a derivation that could drift. ``execution/ranking.py`` imports it.
REVIEWED_NO_FIGURE_STATES: frozenset[str] = frozenset({"ina", "na"})

# #1514 D7 — the canonical stale sentence's prior-value joint. The stale
# display is built around this marker; the stale_coverage_display_invalid
# validator imports it instead of restating the literal.
STALE_PRIOR_VALUE_MARKER = "; the value then was"


class CoverageLabel(BaseModel):
    """One deterministic coverage label for a result row."""

    model_config = ConfigDict(extra="forbid")

    state: CoverageState
    display: str = Field(min_length=1)
    raw_value: Any = None
    reason: CoverageReason | None = None
    qualifier: str | None = None
    prior_academic_year: str | None = None
    prior_display_value: str | None = None


class CoverageBreakdown(BaseModel):
    """Reason-level coverage counts for user-facing disclosure."""

    model_config = ConfigDict(extra="forbid")

    answer_value_count: int = Field(default=0, ge=0)
    issue_not_addressed_count: int = Field(default=0, ge=0)
    not_applicable_count: int = Field(default=0, ge=0)
    metric_not_reviewed_count: int = Field(default=0, ge=0)
    district_not_reviewed_count: int = Field(default=0, ge=0)
    stale_recent_answer_count: int = Field(default=0, ge=0)
    non_numeric_rank_exclusion_count: int = Field(default=0, ge=0)
    out_of_universe_count: int = Field(default=0, ge=0)
    profile_field_value_count: int = Field(default=0, ge=0)
    covered_universe_count: int = Field(default=0, ge=0)
    count_summary_count: int = Field(default=0, ge=0)
    categorical_value_count: int = Field(default=0, ge=0)
    unavailable_count: int = Field(default=0, ge=0)


class CoverageFrame(BaseModel):
    """Aggregate coverage counts for a deterministic result artifact."""

    model_config = ConfigDict(extra="forbid")

    universe_count: int = Field(ge=0)
    in_scope_count: int = Field(ge=0)
    addressed_count: int = Field(ge=0)
    real_data_count: int = Field(ge=0)
    not_reviewed_count: int = Field(ge=0)
    out_of_universe_count: int = Field(ge=0)
    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    sparse: bool = False
    sparse_disclosure: str | None = None
    breakdown: CoverageBreakdown = Field(default_factory=CoverageBreakdown)

    @property
    def current_numeric_count(self) -> int:
        """Count of districts with current reviewed NUMERIC values.

        Excludes non_numeric_rank_exclusion rows (state=="covered" but
        the value is a text answer that cannot be ranked numerically).
        This is the honest numerator for "N of M districts have current
        numeric data" sentences; real_data_count remains the authoritative
        covered-rows total used by count-operation denominators.
        """
        return max(
            0,
            self.real_data_count - self.breakdown.non_numeric_rank_exclusion_count,
        )

    @model_validator(mode="after")
    def populate_legacy_breakdown(self) -> "CoverageFrame":
        """Keep old test fixtures useful while new code emits reason counts."""

        if any(self.breakdown.model_dump().values()):
            return self
        self.breakdown = CoverageBreakdown(
            answer_value_count=self.real_data_count,
            metric_not_reviewed_count=self.not_reviewed_count,
            out_of_universe_count=self.out_of_universe_count,
        )
        return self


class SparseCoverageMetadata(BaseModel):
    """Shared sparse-coverage decision for artifacts and validators."""

    model_config = ConfigDict(extra="forbid")

    coverage_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    sparse: bool = False
    sparse_disclosure: str | None = None


# #1514 retired the "Older year only" (stale_recent_answer) and
# "Out of Pathfinder" (out_of_universe) short labels: those reasons never
# render as table cells anymore — they are voiced as the canonical narrative
# sentences instead.
_SHORT_LABEL_BY_REASON: dict[str, str] = {
    "metric_not_reviewed": "Not reviewed",
    "district_not_reviewed": "Not reviewed",
    "issue_not_addressed": "Not addressed",
    "not_applicable": "Not applicable",
}

# #1698: the canonical "Unavailable" answer sentinels. A covered district whose
# only current value is one of these is a reviewed answer with no usable current
# value — it is narrated (state="not_reviewed"/reason="unavailable"), never
# rendered as a table/chart/CSV cell. Lives here so every surface (count,
# ranking, lookup, peer) inherits the same routing through
# ``coverage_label_for_answer`` rather than each path re-deriving it.
_UNAVAILABLE_VALUES = {
    "unavailable",
    "not available",
    "nctq unavailable",
}


def is_answer_state(state: str | None) -> bool:
    """Return whether a coverage state carries an answer (value, INA, or N/A)."""

    return state in ANSWER_COVERAGE_STATES


def is_rendered_answer_state(state: str | None) -> bool:
    """Row-level answer predicate: ``None`` renders as an answer.

    The pre-labeling convention: a ``None`` coverage_state marks a row built
    before (or without) coverage labeling — legacy fixtures and aggregate rows
    that never carried a label. Tables, charts, and CSV data rows all treat
    those rows as answers, so every surface that partitions *rows* (rather
    than already-labeled states) must use this predicate — never a local
    ``state is None or is_answer_state(state)`` restatement.
    """

    return state is None or state in ANSWER_COVERAGE_STATES


def is_stale_prior_value_row(row: object) -> bool:
    """#1826 — a not-reviewed row that carries a renderable prior-year value.

    A ``stale_recent_answer`` row was reviewed in an earlier year and holds that
    prior figure on ``coverage_prior_display_value`` (with the year on
    ``coverage_prior_academic_year``). #1514 made lookup tables answers-only,
    which demoted these rows to prose — so a state-scoped "starting salaries in
    Florida" showed the one current district and hid 14 real prior-year values.
    This row-level predicate is the *render-inclusion* complement that lets the
    single-metric lookup surface put those rows back in the table.

    Deliberately NOT folded into ``is_rendered_answer_state``: that predicate
    answers "does this row hold CURRENT reviewed data?" — the coverage-summary
    honesty question a stale row must still answer *no* to (it stays counted as
    a not-reviewed gap). This one answers "does this row carry a usable prior
    value worth a rendered cell?". The two diverged when #1514 partitioned the
    table; #1826 reunites the render gate with the prose-demote path that
    already knew how to display a stale value — without un-labeling staleness.
    """

    return (
        getattr(row, "coverage_reason", None) == "stale_recent_answer"
        and bool(getattr(row, "coverage_prior_display_value", None))
    )


def short_coverage_label(reason: str | None, fallback: str) -> str:
    """Short label for table cells; falls back to the full display string."""

    return _SHORT_LABEL_BY_REASON.get(reason or "", fallback)


# Coverage-gap *display* sentinels: the canonical strings a row's
# ``display_value`` carries when NCTQ holds no numeric answer for that
# district/metric (INA, not-applicable, not-reviewed, out-of-universe).
# Built from the constants above — never restate the literals — so the one
# authority for "this cell means no value exists" lives here. The list mixes
# exact short labels (table cells) and the full narrative sentences (lookup
# rows). Recognition is prefix/casefold-based via ``is_coverage_gap_display``.
_COVERAGE_GAP_DISPLAY_PREFIXES: tuple[str, ...] = (
    _INA_DISPLAY,  # "Issue not addressed in the documents reviewed."
    "Issue not addressed",  # the raw cell/value variant before display normalization
    *(label for label in _SHORT_LABEL_BY_REASON.values()),  # Not reviewed / Not addressed / Not applicable
    "Not applicable for",  # _not_applicable_display(...) sentences
    "N/A",  # raw stored not-applicable values (see coverage_label_for_answer)
    "NCTQ hasn't reviewed",  # coverage_label_for_missing_answer sentences
    "NCTQ last reviewed",  # coverage_label_for_stale_answer sentences
)
_COVERAGE_GAP_DISPLAY_SUFFIXES: tuple[str, ...] = (
    "is not in the District Policy Pathfinder.",  # out-of-universe sentence
)


def is_coverage_gap_display(display: str | None) -> bool:
    """Return whether a row ``display_value`` is a coverage-gap sentinel.

    A coverage-gap sentinel means NCTQ holds no numeric value for that
    district/metric — the cell narrates an INA / not-applicable /
    not-reviewed / out-of-universe state rather than carrying a number.
    Numeric deterministic checks (e.g. ``sort_descending``) call this to skip
    such rows instead of treating them as a parse failure: the absence of a
    value is not a non-numeric *error*.
    """

    if not display:
        return False
    normalized = display.strip()
    if not normalized:
        return False
    lowered = normalized.casefold()
    if any(lowered.startswith(p.casefold()) for p in _COVERAGE_GAP_DISPLAY_PREFIXES):
        return True
    return any(lowered.endswith(s.casefold()) for s in _COVERAGE_GAP_DISPLAY_SUFFIXES)


def coverage_label_for_answer(
    value: object,
    *,
    district_name: str,
    metric_name: str,
    academic_year: str,
) -> CoverageLabel:
    """Classify one stored policy answer value into a coverage label."""

    if value is None:
        return coverage_label_for_missing_answer(
            district_name=district_name,
            metric_name=metric_name,
            academic_year=academic_year,
            district_has_current_year_rows=True,
        )

    display_value = format_policy_value(value)
    normalized = display_value.strip().casefold()
    if not normalized:
        return coverage_label_for_missing_answer(
            district_name=district_name,
            metric_name=metric_name,
            academic_year=academic_year,
            district_has_current_year_rows=True,
        )

    if normalized == "ina" or normalized.startswith("issue not addressed"):
        return CoverageLabel(
            state="ina",
            display=_INA_DISPLAY,
            raw_value=value,
            reason="issue_not_addressed",
        )

    # "Source unavailable for recoding" is a data-provenance flag, not a policy
    # answer — it means no citable source exists to classify the district's
    # policy. Treat it as not_reviewed so it voices as a narrative sentence
    # rather than appearing as a table cell (#1514, Fix 3).
    if "source unavailable" in normalized:
        coverage_subject = _coverage_subject(metric_name, None)
        return CoverageLabel(
            state="not_reviewed",
            display=(
                f"NCTQ hasn't reviewed {district_name} for {coverage_subject} "
                f"({academic_year} data not yet reviewed)."
            ),
            reason="metric_not_reviewed",
        )

    if normalized == "n/a" or normalized.startswith("n/a -"):
        qualifier = _extract_qualified_na(display_value, prefix="N/A")
        return CoverageLabel(
            state="na",
            display=_not_applicable_display(district_name, qualifier),
            raw_value=value,
            reason="not_applicable",
            qualifier=qualifier,
        )

    if normalized.startswith("not applicable"):
        qualifier = _extract_not_applicable_qualifier(display_value)
        return CoverageLabel(
            state="na",
            display=_not_applicable_display(district_name, qualifier),
            raw_value=value,
            reason="not_applicable",
            qualifier=qualifier,
        )

    if normalized in _UNAVAILABLE_VALUES:
        # #1698: a current-year "Unavailable" is a reviewed answer with no usable
        # value — narrate it, never cell it. (Was previously remapped only in the
        # count path; the ranking path let it fall through to "covered" and
        # rendered the literal "Unavailable" cell — a count-vs-ranking desync.)
        return CoverageLabel(
            state="not_reviewed",
            display="Unavailable",
            raw_value=value,
            reason="unavailable",
        )

    return CoverageLabel(
        state="covered",
        display=display_value,
        raw_value=value,
        reason="answer_value",
    )


def coverage_label_for_missing_answer(
    *,
    district_name: str,
    metric_name: str,
    academic_year: str,
    district_has_current_year_rows: bool,
    metric_topic: str | None = None,
) -> CoverageLabel:
    """Classify a missing district/metric row without treating absence as prose."""

    if district_has_current_year_rows:
        coverage_subject = _coverage_subject(metric_name, metric_topic)
        return CoverageLabel(
            state="not_reviewed",
            display=(
                f"NCTQ hasn't reviewed {district_name} for {coverage_subject} "
                f"({academic_year} data not yet reviewed)."
            ),
            reason="metric_not_reviewed",
        )
    return CoverageLabel(
        state="not_reviewed",
        display=f"NCTQ hasn't reviewed {district_name} ({academic_year} data not yet reviewed).",
        reason="district_not_reviewed",
    )


def coverage_label_for_out_of_universe(
    district_name: str,
    *,
    state: str | None = None,
) -> CoverageLabel:
    """Classify a requested name outside the District Policy Pathfinder.

    #1514 D6: pass ``state`` (same 2-letter form the tables' State column
    uses) when the call site can derive it — a typed trailing qualifier or
    the selection's lone requested state — so the canonical sentence reads
    "{name}, {state} is not in the District Policy Pathfinder." The
    stateless sentence is the data-honest fallback.
    """

    display_name = district_name or "This district"
    if state:
        display_name = f"{display_name}, {state}"
    return CoverageLabel(
        state="out_of_universe",
        display=f"{display_name} is not in the District Policy Pathfinder.",
        raw_value=district_name,
        reason="out_of_universe",
    )


def coverage_label_for_stale_answer(
    value: object,
    *,
    district_name: str,
    metric_name: str,
    current_academic_year: str,
    prior_academic_year: str,
    metric_topic: str | None = None,
) -> CoverageLabel:
    """Classify a prior-year answer as disclosure, not current data."""

    display_value = format_policy_value(value)
    coverage_subject = _coverage_subject(metric_name, metric_topic)
    return CoverageLabel(
        state="not_reviewed",
        display=(
            f"NCTQ last reviewed {district_name} for {coverage_subject} in "
            f"{prior_academic_year}{STALE_PRIOR_VALUE_MARKER} {display_value}."
        ),
        raw_value=value,
        reason="stale_recent_answer",
        prior_academic_year=prior_academic_year,
        prior_display_value=display_value,
    )


def coverage_frame_from_labels(labels: Iterable[CoverageLabel]) -> CoverageFrame:
    """Build aggregate coverage counts from row-level labels."""

    materialized = list(labels)
    state_counts = {
        state: sum(1 for label in materialized if label.state == state)
        for state in ("covered", "ina", "na", "not_reviewed", "out_of_universe")
    }
    return coverage_frame_from_state_counts(
        state_counts,
        universe_count=len(materialized),
        breakdown=coverage_breakdown_from_labels(materialized),
    )


def coverage_frame_from_state_counts(
    state_counts: Mapping[str, int],
    *,
    universe_count: int,
    breakdown: CoverageBreakdown | None = None,
) -> CoverageFrame:
    """Build a coverage frame from already-counted coverage states."""

    covered_count = state_counts.get("covered", 0)
    ina_count = state_counts.get("ina", 0)
    na_count = state_counts.get("na", 0)
    not_reviewed_count = state_counts.get("not_reviewed", 0)
    out_of_universe_count = state_counts.get("out_of_universe", 0)
    in_scope_count = covered_count + ina_count + na_count + not_reviewed_count
    sparse = sparse_coverage_metadata(
        real_data_count=covered_count,
        in_scope_count=in_scope_count,
    )
    return CoverageFrame(
        universe_count=universe_count,
        in_scope_count=in_scope_count,
        addressed_count=covered_count + ina_count + na_count,
        real_data_count=covered_count,
        not_reviewed_count=not_reviewed_count,
        out_of_universe_count=out_of_universe_count,
        coverage_ratio=sparse.coverage_ratio,
        sparse=sparse.sparse,
        sparse_disclosure=sparse.sparse_disclosure,
        breakdown=breakdown
        or CoverageBreakdown(
            answer_value_count=covered_count,
            issue_not_addressed_count=ina_count,
            not_applicable_count=na_count,
            metric_not_reviewed_count=not_reviewed_count,
            out_of_universe_count=out_of_universe_count,
        ),
    )


def coverage_breakdown_from_labels(
    labels: Iterable[CoverageLabel],
) -> CoverageBreakdown:
    """Build a reason-level breakdown from deterministic row labels."""

    counts: dict[str, int] = {}
    for label in labels:
        reason = label.reason or _default_reason_for_state(label.state)
        key = f"{reason}_count"
        counts[key] = counts.get(key, 0) + 1
    return CoverageBreakdown(**counts)


def sparse_coverage_metadata(
    *,
    real_data_count: int,
    in_scope_count: int,
) -> SparseCoverageMetadata:
    """Return the canonical sparse-coverage decision and disclosure."""

    coverage_ratio = real_data_count / in_scope_count if in_scope_count else 0.0
    sparse = (
        in_scope_count >= SPARSE_COVERAGE_MIN_IN_SCOPE_CELLS
        and coverage_ratio < SPARSE_COVERAGE_RATIO_THRESHOLD
    )
    return SparseCoverageMetadata(
        coverage_ratio=coverage_ratio,
        sparse=sparse,
        sparse_disclosure=(
            f"Sparse coverage: {real_data_count} of {in_scope_count} in-scope "
            "cells have current reviewed data."
            if sparse
            else None
        ),
    )


def format_policy_value(value: object) -> str:
    """Format a stored policy value for deterministic row display."""

    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value).strip()


def _extract_qualified_na(value: str, *, prefix: str) -> str | None:
    remainder = value[len(prefix) :].strip()
    if remainder.startswith("-"):
        return _clean_qualifier(remainder[1:])
    return None


def _extract_not_applicable_qualifier(value: str) -> str | None:
    prefix_length = len("Not applicable")
    remainder = value[prefix_length:].strip()
    if remainder.startswith("-") or remainder.startswith(":"):
        return _clean_qualifier(remainder[1:])
    return None


def _clean_qualifier(value: str) -> str | None:
    cleaned = value.strip().rstrip(".")
    return cleaned or None


def _not_applicable_display(district_name: str, qualifier: str | None) -> str:
    if qualifier:
        return f"Not applicable for {district_name}: {qualifier}."
    return f"Not applicable for {district_name}."


def _coverage_subject(metric_name: str, metric_topic: str | None) -> str:
    topic = (metric_topic or "").strip()
    if topic:
        return topic
    return metric_name


def _default_reason_for_state(state: CoverageState) -> CoverageReason:
    if state == "covered":
        return "answer_value"
    if state == "ina":
        return "issue_not_addressed"
    if state == "na":
        return "not_applicable"
    if state == "out_of_universe":
        return "out_of_universe"
    return "metric_not_reviewed"
