"""Tests for salary-disclosure primitives.

The phrase authority and disclosure copy live in governed catalog/renderer
metadata. Runtime code keeps only metric-shape detection helpers.
"""

from __future__ import annotations

import compass_backend.execution._salary_disclosure as salary_disclosure
from compass_backend.execution._salary_disclosure import is_bachelor_starting_salary_metric
from compass_backend.execution._text_utils import metric_phrase_key


def test_salary_disclosure_module_has_no_phrase_authority_or_copy() -> None:
    assert not hasattr(salary_disclosure, "BACHELOR_STARTING_SALARY_PHRASES")
    assert not hasattr(salary_disclosure, "BACHELOR_STARTING_SALARY_NOTE")


def test_is_bachelor_starting_salary_metric_positive_cases() -> None:
    assert is_bachelor_starting_salary_metric(
        "Average starting salary for first-year teachers with a bachelor's degree"
    )
    assert is_bachelor_starting_salary_metric(
        "starting_salary_first_year_teacher_bachelor"
    )


def test_is_bachelor_starting_salary_metric_negative_cases() -> None:
    assert not is_bachelor_starting_salary_metric(
        "Average starting salary for first-year teachers with a master's degree"
    )
    assert not is_bachelor_starting_salary_metric("Maximum teacher salary")
    assert not is_bachelor_starting_salary_metric(
        "Average bachelor's-degree teacher pay"
    )  # missing "first year"
    assert not is_bachelor_starting_salary_metric("")


def test_metric_phrase_key_collapses_punctuation_and_casefolds() -> None:
    assert metric_phrase_key("Starting Salary!") == "starting salary"
    assert metric_phrase_key("first-year teacher pay") == "first year teacher pay"
    assert metric_phrase_key("   multiple   spaces   ") == "multiple spaces"
    assert metric_phrase_key("") == ""
