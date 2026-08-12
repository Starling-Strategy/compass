"""Architecture tests for the deterministic query executor boundary."""

from __future__ import annotations

from compass_backend.db import ReadOnlyCatalogRepository, ReadOnlyPolicyAnswerRepository
from compass_backend.execution import (
    DeterministicQueryExecutor,
    ExecutionOutcome,
    MetricAnswerRow,
    PolicyAnswerRepository,
    QueryExecutor,
)


def test_execution_public_api_uses_query_not_ranking_names() -> None:
    import compass_backend.execution as execution

    assert "DeterministicQueryExecutor" in execution.__all__
    assert "PolicyAnswerRepository" in execution.__all__
    assert "DeterministicRankingExecutor" not in execution.__all__
    assert "RankingRepository" not in execution.__all__


def test_db_public_api_uses_policy_answer_repository_name() -> None:
    import compass_backend.db as db

    assert "ReadOnlyCatalogRepository" in db.__all__
    assert "ReadOnlyPolicyAnswerRepository" in db.__all__
    assert "ReadOnlyRankingRepository" not in db.__all__


def test_generic_executor_contract_names_are_importable() -> None:
    assert DeterministicQueryExecutor is not None
    assert PolicyAnswerRepository is not None
    assert QueryExecutor is not None
    assert ExecutionOutcome is not None
    assert MetricAnswerRow is not None
    assert ReadOnlyCatalogRepository is not None
    assert ReadOnlyPolicyAnswerRepository is not None
