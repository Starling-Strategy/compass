"""Deterministic execution layer for the fresh Compass API."""

from compass_backend.catalog import (
    DistrictCandidate,
    DistrictResolution,
    MetricCandidate,
    normalize_district_name_for_resolution,
)

from .executor import DeterministicQueryExecutor
from .types import (
    EXECUTION_OUTCOME_TYPES,
    ExecutionClarification,
    ExecutionOutcome,
    ExecutionRefusal,
    ExecutionSuccess,
    MetricAnswerRow,
    PolicyAnswerRepository,
    QueryExecutor,
)

__all__ = [
    "DeterministicQueryExecutor",
    "DistrictCandidate",
    "DistrictResolution",
    "EXECUTION_OUTCOME_TYPES",
    "ExecutionClarification",
    "ExecutionOutcome",
    "ExecutionRefusal",
    "ExecutionSuccess",
    "MetricAnswerRow",
    "MetricCandidate",
    "PolicyAnswerRepository",
    "QueryExecutor",
    "normalize_district_name_for_resolution",
]
