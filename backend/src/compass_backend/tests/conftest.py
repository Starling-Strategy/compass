"""Pytest configuration for fresh Compass API tests."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import pytest

if TYPE_CHECKING:
    from compass_backend.db.rows import CriterionRecord
    from compass_backend.quality._evaluator_context import CompassEvaluatorContext

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def pytest_addoption(parser: pytest.Parser) -> None:
    try:
        parser.addoption(
            "--run-live-db",
            action="store_true",
            default=False,
            help="Run optional live staging-database smoke tests.",
        )
    except ValueError:
        pass  # already registered by another conftest in combined test runs


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live_db: optional tests that require explicit --run-live-db and DB settings",
    )


@pytest.fixture(autouse=True)
def _reset_catalog_caches():
    """Clear the process-lifetime catalog caches before each test.

    ``db.catalog`` caches ``catalog_aliases`` and the ``nces_districts`` column
    set for the life of the process (#1357 C1/C2). Those module-level caches
    would otherwise bleed across tests and make per-call / query-count
    assertions order-dependent, so reset them before every test.
    """
    from compass_backend.db.catalog import (
        reset_alias_cache,
        reset_nces_fields_cache,
    )

    reset_alias_cache()
    reset_nces_fields_cache()
    yield


# ─── Shared domain factory fixtures (#1590) ───────────────────────────────────
#
# Many test modules hand-rolled near-identical `_ctx(...)` /
# `_make_criterion(...)` builders for CompassEvaluatorContext and
# CriterionRecord. When either model gained a required field, every copy had to
# be patched, and they had already drifted (different default session_ids,
# positional-vs-keyword signatures, a near-identical `_DEFAULT_PAYLOADS` dict
# copied into ~7 files). These two factories hold the single shared default
# shape; per-file specifics are passed as explicit `make(**overrides)` at the
# call site (or via a thin module-level shim), so each test keeps its exact
# effective values.
#
# The factories are exposed two ways:
#   * importable plain functions (`make_evaluator_context` /
#     `make_criterion_record`) — so a module-level helper in a test file can
#     delegate to them without taking a fixture argument; and
#   * fixtures (`evaluator_context` / `criterion_record`) that simply return
#     those callables, for tests that prefer the fixture-injection form.

# Default criterion payloads keyed by check_type. The discriminated payload
# union needs a check_type-appropriate body (the row-level check_type is copied
# onto the payload by CriterionRecord._inject_payload_discriminator). This is
# the shape the per-file `_DEFAULT_PAYLOADS` dicts converged on. The
# `judge_prompt` text is arbitrary for tests that don't read the rubric; modules
# that depend on a specific judge_prompt pass `payload=` explicitly.
_DEFAULT_CRITERION_PAYLOADS: dict[str, dict[str, Any]] = {
    "deterministic": {"validator_name": "test_validator"},
    "judge_prompt": {"judge_prompt": "test rubric"},
    "span_assertion": {},
    "scenario_fit": {"judge_prompt_template": "{answer_text}"},
}


def make_evaluator_context(**overrides: Any) -> CompassEvaluatorContext:
    """Build a CompassEvaluatorContext with the most-common hand-rolled defaults.

    Defaults: a fixed UUID session, ``turn_index=0``, ``triggered_by="user_turn"``.
    Pass any CompassEvaluatorContext field as a keyword override; per-file
    helpers supply their own ``session_id`` / ``answer_text`` / etc. so each
    call site keeps its exact effective values.
    """
    from compass_backend.quality._evaluator_context import CompassEvaluatorContext

    params: dict[str, Any] = {
        "session_id": "11111111-1111-1111-1111-111111111111",
        "turn_index": 0,
        "triggered_by": "user_turn",
    }
    params.update(overrides)
    return CompassEvaluatorContext(**params)


def make_criterion_record(**overrides: Any) -> CriterionRecord:
    """Build a CriterionRecord with the most-common hand-rolled defaults.

    When ``payload`` is not supplied, a check_type-appropriate default body is
    used (``_DEFAULT_CRITERION_PAYLOADS``). ``version_hash`` defaults to
    ``hash-<criterion_code>-v1`` (the most common pattern). Pass any
    CriterionRecord field — including ``check_type``, ``category``, ``severity``,
    ``priority``, ``is_mandatory_global``, ``id``, ``payload`` — as a keyword
    override.
    """
    from compass_backend.db.rows import CriterionRecord

    check_type = overrides.get("check_type", "deterministic")
    criterion_code = overrides.get("criterion_code", "test_criterion")
    params: dict[str, Any] = {
        "id": 1,
        "criterion_code": criterion_code,
        "text": f"Test criterion {criterion_code}",
        "category": "accuracy",
        "check_type": check_type,
        "severity": "warn",
        "priority": 10,
        "is_mandatory_global": False,
        "scenario_ids": [],
        "topic_tags": [],
        "intent_tags": [],
        "payload": dict(_DEFAULT_CRITERION_PAYLOADS[check_type]),
        "version_hash": f"hash-{criterion_code}-v1",
        "active": True,
    }
    params.update(overrides)
    return CriterionRecord(**params)


@pytest.fixture
def evaluator_context() -> Callable[..., CompassEvaluatorContext]:
    """Return the `make_evaluator_context(**overrides)` factory (see #1590)."""
    return make_evaluator_context


@pytest.fixture
def criterion_record() -> Callable[..., CriterionRecord]:
    """Return the `make_criterion_record(**overrides)` factory (see #1590)."""
    return make_criterion_record
