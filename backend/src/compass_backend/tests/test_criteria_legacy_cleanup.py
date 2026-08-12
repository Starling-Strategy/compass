"""Guards for the active compass.criteria evaluator path.

The retired GoldenCriterion hierarchy belongs in history only; active runtime
evaluation should expose CriterionRecord-backed factories.
"""

from __future__ import annotations


def test_quality_criteria_does_not_export_golden_criterion_runtime_path() -> None:
    import compass_backend.quality.criteria as criteria

    exported = set(getattr(criteria, "__all__", ()))
    assert "GoldenCriterion" not in exported
    assert "criterion_to_evaluator" not in exported
    assert not hasattr(criteria, "GoldenCriterion")
    assert not hasattr(criteria, "criterion_to_evaluator")
    assert hasattr(criteria, "CriterionRecord")
    assert hasattr(criteria, "criterion_record_to_evaluator")
