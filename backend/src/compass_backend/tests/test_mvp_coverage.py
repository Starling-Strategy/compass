"""Tests for SSN-224 coverage-state labeling rules."""

from compass_backend.artifacts.coverage import (
    ANSWER_COVERAGE_STATES,
    STALE_PRIOR_VALUE_MARKER,
    CoverageBreakdown,
    CoverageFrame,
    coverage_label_for_out_of_universe,
    coverage_label_for_answer,
    coverage_label_for_missing_answer,
    coverage_label_for_stale_answer,
    coverage_frame_from_labels,
    coverage_frame_from_state_counts,
    is_answer_state,
    short_coverage_label,
    sparse_coverage_metadata,
)


def test_coverage_label_marks_ina_with_canonical_phrase() -> None:
    label = coverage_label_for_answer(
        "INA",
        district_name="Alpha",
        metric_name="Collective bargaining status",
        academic_year="2024 - 2025",
    )

    assert label.state == "ina"
    assert label.display == "Issue not addressed in the documents reviewed."
    assert label.qualifier is None


def test_coverage_label_marks_issue_not_addressed_as_ina() -> None:
    label = coverage_label_for_answer(
        "Issue not addressed in the agreement.",
        district_name="Alpha",
        metric_name="Collective bargaining status",
        academic_year="2024 - 2025",
    )

    assert label.state == "ina"
    assert label.display == "Issue not addressed in the documents reviewed."


def test_coverage_label_marks_qualified_na_with_district_phrase() -> None:
    label = coverage_label_for_answer(
        "N/A - district does not have collective bargaining",
        district_name="Alpha",
        metric_name="Collective bargaining status",
        academic_year="2024 - 2025",
    )

    assert label.state == "na"
    assert label.qualifier == "district does not have collective bargaining"
    assert label.display == (
        "Not applicable for Alpha: district does not have collective bargaining."
    )


def test_coverage_label_marks_not_applicable_prefix_as_na() -> None:
    label = coverage_label_for_answer(
        "Not applicable - no salary schedule",
        district_name="Bravo",
        metric_name="Average teacher starting salary",
        academic_year="2024 - 2025",
    )

    assert label.state == "na"
    assert label.display == "Not applicable for Bravo: no salary schedule."


def test_coverage_label_preserves_covered_display_values() -> None:
    label = coverage_label_for_answer(
        True,
        district_name="Alpha",
        metric_name="Collective bargaining status",
        academic_year="2024 - 2025",
    )

    assert label.state == "covered"
    assert label.display == "Yes"


def test_coverage_label_routes_current_year_unavailable_to_not_reviewed() -> None:
    """#1698: a covered district whose current value is the canonical
    "Unavailable" sentinel is a reviewed answer with no usable current value —
    it must be narrated (state=not_reviewed/reason="unavailable"), never rendered
    as a literal table/chart/CSV cell. Single source for both the count path and
    the ranking path (which previously diverged: count narrated, ranking celled).
    """
    for raw in ("Unavailable", "unavailable", "Not available", "NCTQ Unavailable"):
        label = coverage_label_for_answer(
            raw,
            district_name="Houston ISD",
            metric_name="Minimum number of formal observations",
            academic_year="2024 - 2025",
        )
        assert label.state == "not_reviewed", raw
        assert label.reason == "unavailable", raw
        assert label.display == "Unavailable", raw


def test_missing_metric_row_for_reviewed_district_is_metric_level_not_reviewed() -> None:
    label = coverage_label_for_missing_answer(
        district_name="Alpha",
        metric_name="Collective bargaining status",
        academic_year="2024 - 2025",
        district_has_current_year_rows=True,
    )

    assert label.state == "not_reviewed"
    assert label.reason == "metric_not_reviewed"
    assert label.display == (
        "NCTQ hasn't reviewed Alpha for Collective bargaining status "
        "(2024 - 2025 data not yet reviewed)."
    )


def test_missing_metric_row_for_unreviewed_district_is_district_level_not_reviewed() -> None:
    label = coverage_label_for_missing_answer(
        district_name="Charlie",
        metric_name="Collective bargaining status",
        academic_year="2024 - 2025",
        district_has_current_year_rows=False,
    )

    assert label.state == "not_reviewed"
    assert label.reason == "district_not_reviewed"
    assert label.display == "NCTQ hasn't reviewed Charlie (2024 - 2025 data not yet reviewed)."


def test_out_of_universe_label_uses_canonical_catalog_phrase() -> None:
    """Stateless fallback (#1514 D6): no derivable state -> no state mention."""

    label = coverage_label_for_out_of_universe("Unknown District")

    assert label.state == "out_of_universe"
    assert label.reason == "out_of_universe"
    assert label.display == "Unknown District is not in the District Policy Pathfinder."


def test_out_of_universe_label_mentions_state_when_provided() -> None:
    """#1514 D6 — '{name}, {state} is not in the District Policy Pathfinder.'"""

    label = coverage_label_for_out_of_universe("Cincinnati", state="OH")

    assert label.state == "out_of_universe"
    assert label.reason == "out_of_universe"
    assert label.raw_value == "Cincinnati"
    assert label.display == "Cincinnati, OH is not in the District Policy Pathfinder."


def test_stale_answer_label_discloses_prior_year_without_becoming_current_data() -> None:
    label = coverage_label_for_stale_answer(
        "$50,000",
        district_name="Alpha",
        metric_name="Average teacher starting salary",
        current_academic_year="2024 - 2025",
        prior_academic_year="2023 - 2024",
    )

    assert label.state == "not_reviewed"
    assert label.reason == "stale_recent_answer"
    assert label.raw_value == "$50,000"
    assert label.prior_academic_year == "2023 - 2024"
    assert label.prior_display_value == "$50,000"
    # #1514 D7 — the canonical stale sentence, built around the marker the
    # stale_coverage_display_invalid validator keys on.
    assert STALE_PRIOR_VALUE_MARKER in label.display
    assert label.display == (
        "NCTQ last reviewed Alpha for Average teacher starting salary in "
        "2023 - 2024; the value then was $50,000."
    )


def test_missing_and_stale_labels_prefer_metric_topic_when_available() -> None:
    long_metric_name = (
        "Maximum annual salary at the highest degree lane and highest step on "
        "the district salary schedule"
    )

    missing = coverage_label_for_missing_answer(
        district_name="Alpha",
        metric_name=long_metric_name,
        metric_topic="salary schedules",
        academic_year="2024 - 2025",
        district_has_current_year_rows=True,
    )
    stale = coverage_label_for_stale_answer(
        "$91,000",
        district_name="Alpha",
        metric_name=long_metric_name,
        metric_topic="salary schedules",
        current_academic_year="2024 - 2025",
        prior_academic_year="2023 - 2024",
    )

    assert long_metric_name not in missing.display
    assert missing.display == (
        "NCTQ hasn't reviewed Alpha for salary schedules (2024 - 2025 data not yet reviewed)."
    )
    assert long_metric_name not in stale.display
    assert stale.display == (
        "NCTQ last reviewed Alpha for salary schedules in 2023 - 2024; "
        "the value then was $91,000."
    )


def test_coverage_frame_breakdown_keeps_not_reviewed_reasons_distinct() -> None:
    labels = [
        coverage_label_for_answer(
            "Yes",
            district_name="Alpha",
            metric_name="Collective bargaining status",
            academic_year="2024 - 2025",
        ),
        coverage_label_for_answer(
            "INA",
            district_name="Bravo",
            metric_name="Collective bargaining status",
            academic_year="2024 - 2025",
        ),
        coverage_label_for_answer(
            "N/A - district has no collective bargaining",
            district_name="Charlie",
            metric_name="Collective bargaining status",
            academic_year="2024 - 2025",
        ),
        coverage_label_for_missing_answer(
            district_name="Delta",
            metric_name="Collective bargaining status",
            academic_year="2024 - 2025",
            district_has_current_year_rows=True,
        ),
        coverage_label_for_missing_answer(
            district_name="Echo",
            metric_name="Collective bargaining status",
            academic_year="2024 - 2025",
            district_has_current_year_rows=False,
        ),
        coverage_label_for_stale_answer(
            "No",
            district_name="Foxtrot",
            metric_name="Collective bargaining status",
            current_academic_year="2024 - 2025",
            prior_academic_year="2023 - 2024",
        ),
        coverage_label_for_out_of_universe("Unknown District"),
    ]

    frame = coverage_frame_from_labels(labels)

    assert frame.universe_count == 7
    assert frame.in_scope_count == 6
    assert frame.real_data_count == 1
    assert frame.not_reviewed_count == 3
    assert frame.out_of_universe_count == 1
    assert frame.breakdown.answer_value_count == 1
    assert frame.breakdown.issue_not_addressed_count == 1
    assert frame.breakdown.not_applicable_count == 1
    assert frame.breakdown.metric_not_reviewed_count == 1
    assert frame.breakdown.district_not_reviewed_count == 1
    assert frame.breakdown.stale_recent_answer_count == 1
    assert frame.breakdown.out_of_universe_count == 1


def test_sparse_coverage_frame_sets_canonical_disclosure() -> None:
    labels = [
        coverage_label_for_answer(
            "Yes",
            district_name="Alpha",
            metric_name="Collective bargaining status",
            academic_year="2024 - 2025",
        ),
        coverage_label_for_missing_answer(
            district_name="Bravo",
            metric_name="Collective bargaining status",
            academic_year="2024 - 2025",
            district_has_current_year_rows=True,
        ),
        coverage_label_for_missing_answer(
            district_name="Charlie",
            metric_name="Collective bargaining status",
            academic_year="2024 - 2025",
            district_has_current_year_rows=True,
        ),
    ]

    frame = coverage_frame_from_labels(labels)

    assert frame.coverage_ratio == 1 / 3
    assert frame.sparse is True
    assert frame.sparse_disclosure == (
        "Sparse coverage: 1 of 3 in-scope cells have current reviewed data."
    )


def test_sparse_coverage_policy_helper_is_the_single_threshold_source() -> None:
    metadata = sparse_coverage_metadata(real_data_count=1, in_scope_count=3)

    assert metadata.coverage_ratio == 1 / 3
    assert metadata.sparse is True
    assert metadata.sparse_disclosure == (
        "Sparse coverage: 1 of 3 in-scope cells have current reviewed data."
    )

    frame = coverage_frame_from_state_counts(
        {
            "covered": 1,
            "ina": 0,
            "na": 0,
            "not_reviewed": 2,
            "out_of_universe": 0,
        },
        universe_count=3,
    )

    assert frame.coverage_ratio == metadata.coverage_ratio
    assert frame.sparse == metadata.sparse
    assert frame.sparse_disclosure == metadata.sparse_disclosure


# ─── short_coverage_label (PR-A #741) ────────────────────────────────────────


def test_short_coverage_label_returns_short_string_for_each_known_reason() -> None:
    """Every reason that lands in a not-reviewed cell has a short label."""

    assert short_coverage_label("metric_not_reviewed", "long fallback") == "Not reviewed"
    assert short_coverage_label("district_not_reviewed", "long fallback") == "Not reviewed"
    assert short_coverage_label("issue_not_addressed", "long fallback") == "Not addressed"
    assert short_coverage_label("not_applicable", "long fallback") == "Not applicable"


def test_short_coverage_label_falls_back_for_unmapped_or_none_reason() -> None:
    """Unknown reasons (numeric cells, non_numeric_rank_exclusion) keep the fallback."""

    assert short_coverage_label(None, "$60,000") == "$60,000"
    assert short_coverage_label("answer_value", "$60,000") == "$60,000"
    assert short_coverage_label("non_numeric_rank_exclusion", "covered") == "covered"


def test_retired_short_labels_fall_back_to_the_full_narrative_display() -> None:
    """#1514 retired 'Older year only' and 'Out of Pathfinder' — those reasons
    are voiced as canonical narrative sentences, never table-cell labels."""

    assert short_coverage_label("stale_recent_answer", "full sentence") == "full sentence"
    assert short_coverage_label("out_of_universe", "full sentence") == "full sentence"


# ─── is_answer_state / ANSWER_COVERAGE_STATES (#1514 D1) ─────────────────────


def test_is_answer_state_partitions_the_five_coverage_states() -> None:
    """Answer rows (value / INA / N/A) render as data; the other two states
    are voiced as narrative sentences. One predicate, shared everywhere."""

    assert ANSWER_COVERAGE_STATES == frozenset({"covered", "ina", "na"})
    assert is_answer_state("covered") is True
    assert is_answer_state("ina") is True
    assert is_answer_state("na") is True
    assert is_answer_state("not_reviewed") is False
    assert is_answer_state("out_of_universe") is False
    assert is_answer_state(None) is False


# ─── CoverageFrame.current_numeric_count (#1436) ─────────────────────────────


def test_current_numeric_count_excludes_non_numeric_rank_exclusion_rows() -> None:
    """current_numeric_count = real_data_count - non_numeric_rank_exclusion_count.

    When the covered set includes non-rankable text answers, the honest
    numeric count is smaller than real_data_count.  real_data_count is
    kept unchanged so count-operation denominators are unaffected.
    """

    frame = CoverageFrame(
        universe_count=10,
        in_scope_count=10,
        addressed_count=7,
        real_data_count=7,
        not_reviewed_count=3,
        out_of_universe_count=0,
        breakdown=CoverageBreakdown(
            answer_value_count=4,
            non_numeric_rank_exclusion_count=3,
            metric_not_reviewed_count=3,
        ),
    )

    assert frame.real_data_count == 7
    assert frame.current_numeric_count == 4


def test_current_numeric_count_equals_real_data_count_when_all_numeric() -> None:
    """When there are no non-numeric exclusions current_numeric_count == real_data_count."""

    frame = CoverageFrame(
        universe_count=5,
        in_scope_count=5,
        addressed_count=5,
        real_data_count=5,
        not_reviewed_count=0,
        out_of_universe_count=0,
        breakdown=CoverageBreakdown(answer_value_count=5),
    )

    assert frame.current_numeric_count == 5


def test_current_numeric_count_is_zero_floored() -> None:
    """current_numeric_count never goes negative (defensive floor at 0)."""

    # Construct a frame where the breakdown counts are inconsistent with
    # real_data_count — the property must floor at 0 rather than go negative.
    frame = CoverageFrame(
        universe_count=3,
        in_scope_count=3,
        addressed_count=2,
        real_data_count=2,
        not_reviewed_count=1,
        out_of_universe_count=0,
        breakdown=CoverageBreakdown(
            answer_value_count=0,
            non_numeric_rank_exclusion_count=5,  # intentionally > real_data_count
        ),
    )

    assert frame.current_numeric_count == 0
