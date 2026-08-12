"""Tests for the consolidated degree-lane classifier (audit finding #17).

The token-based classifier in ``catalog/degree_lane.py`` is the single source
of truth for "is this metric in the BA lane or the MA lane?". It replaces
duplicated logic that previously lived in ``catalog/resolution.py``
(``_LANE_TOKENS_BA``/``_LANE_TOKENS_MA``) and ``execution/_salary_disclosure.py``
(substring ``"bachelor" in key``).

These tests pin the contract of the new helper directly — broader
integration coverage already exists in ``test_degree_lane.py`` (catalog) and
``test_salary_disclosure.py`` (executor).
"""

from __future__ import annotations

from compass_backend.catalog.degree_lane import classify_metric_lane


def test_classify_metric_lane_bachelor_token() -> None:
    """Whole token ``bachelor`` in the name produces the BA lane."""

    assert (
        classify_metric_lane(
            "Annual base salary for a first year teacher with a bachelor's degree"
        )
        == "ba"
    )


def test_classify_metric_lane_bachelors_token() -> None:
    """Plural ``bachelors`` also resolves to BA."""

    assert classify_metric_lane("first-year bachelors salary") == "ba"


def test_classify_metric_lane_ba_abbreviation() -> None:
    """The bare ``BA`` abbreviation is a BA lane token."""

    assert classify_metric_lane("BA starting salary") == "ba"


def test_classify_metric_lane_master_token() -> None:
    """Whole token ``master`` in the name produces the MA lane."""

    assert (
        classify_metric_lane(
            "Annual base salary for a first year teacher with a master's degree"
        )
        == "ma"
    )


def test_classify_metric_lane_masters_token() -> None:
    """Plural ``masters`` also resolves to MA."""

    assert classify_metric_lane("masters salary lane") == "ma"


def test_classify_metric_lane_ma_abbreviation() -> None:
    """The bare ``MA`` abbreviation is an MA lane token."""

    assert classify_metric_lane("MA starting salary") == "ma"


def test_classify_metric_lane_none_for_unrelated() -> None:
    """A metric with no degree qualifier is lane-neutral."""

    assert classify_metric_lane("Teacher contracted workdays per year") is None


def test_classify_metric_lane_empty_string() -> None:
    """Empty input is lane-neutral, never a token match."""

    assert classify_metric_lane("") is None


def test_classify_metric_lane_rejects_bachelor_substring() -> None:
    """A name containing ``bachelorette`` as a SUBSTRING of a whole token must
    not classify as BA. This is the stricter whole-token contract the audit
    consolidation introduces — see audit finding #17.
    """

    # 'bachelorette' tokenises to itself; it never produces a bare 'bachelor'
    # whole token, so the lane membership check returns None.
    assert classify_metric_lane("bachelorette-themed stipend salary") is None


def test_classify_metric_lane_rejects_master_substring() -> None:
    """Symmetric guarantee for the MA side: ``masterclass`` is not MA."""

    assert classify_metric_lane("masterclass attendance stipend") is None


def test_classify_metric_lane_is_case_insensitive() -> None:
    """Casefolding happens inside the classifier."""

    assert classify_metric_lane("BACHELOR starting salary") == "ba"
    assert classify_metric_lane("Master Salary") == "ma"


def test_classify_metric_lane_splits_on_punctuation() -> None:
    """``bachelor's-degree`` produces the whole token ``bachelor``."""

    assert classify_metric_lane("bachelor's-degree first year salary") == "ba"
