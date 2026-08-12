"""Regression tests for /chat/simple eval-mode header suppression.

When the caller sends ``X-Compass-Eval-Mode: sweep`` the live
``verdict_pipeline.fire_and_forget`` call MUST be suppressed so that
pa-eval's own ``evaluate_and_write`` pass is the only verdict writer for
that turn (single-pass, no duplicates, no gateway contention).

Without the header the existing fire-and-forget behaviour must be preserved.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from compass_backend.api.app import create_app
from compass_backend.api.chat import EVAL_MODE_HEADER, EVAL_MODE_SWEEP
from compass_backend.session import InMemorySessionStore

from compass_backend.tests.test_mvp_chat import (
    FakeQueryExecutor,
    _agent_for_turn,
    _direct_turn,
)
from compass_backend.execution import ExecutionRefusal


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_client_and_mock(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, MagicMock]:
    """Return a TestClient backed by a real (TestModel) planner agent and
    InMemorySessionStore, together with a MagicMock wired over
    ``verdict_pipeline.fire_and_forget`` at the import site in chat.py.

    Using the real TestModel planner (via ``_agent_for_turn``) means
    ``build_chat_response`` runs normally and returns a proper ChatResponse,
    so FastAPI's ``response_model=ChatResponse`` validation is satisfied.
    """
    mock_faf = MagicMock()
    monkeypatch.setattr(
        "compass_backend.api.chat.verdict_pipeline.fire_and_forget",
        mock_faf,
    )

    agent = _agent_for_turn(_direct_turn("Test response."))
    store = InMemorySessionStore()
    executor = FakeQueryExecutor(ExecutionRefusal(message="unused"))
    app = create_app(
        planner_agent=agent,  # type: ignore[arg-type]
        session_store=store,  # type: ignore[arg-type]
        query_executor=executor,  # type: ignore[arg-type]
    )
    return TestClient(app), mock_faf


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_chat_simple_without_header_calls_fire_and_forget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the eval-mode header, fire_and_forget is called exactly once."""
    client, mock_faf = _make_client_and_mock(monkeypatch)

    with client:
        resp = client.post("/api/v1/chat/simple", json={"message": "hi"})

    assert resp.status_code == 200
    mock_faf.assert_called_once()


def test_chat_simple_with_sweep_header_suppresses_fire_and_forget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With X-Compass-Eval-Mode: sweep, fire_and_forget is NOT called."""
    client, mock_faf = _make_client_and_mock(monkeypatch)

    with client:
        resp = client.post(
            "/api/v1/chat/simple",
            json={"message": "hi"},
            headers={EVAL_MODE_HEADER: EVAL_MODE_SWEEP},
        )

    assert resp.status_code == 200
    mock_faf.assert_not_called()


def test_chat_simple_with_sweep_header_case_insensitive_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Header value matching is case-insensitive (e.g. 'Sweep' == 'sweep')."""
    client, mock_faf = _make_client_and_mock(monkeypatch)

    with client:
        resp = client.post(
            "/api/v1/chat/simple",
            json={"message": "hi"},
            headers={EVAL_MODE_HEADER: "Sweep"},
        )

    assert resp.status_code == 200
    mock_faf.assert_not_called()


def test_chat_simple_with_unknown_eval_mode_preserves_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognised eval-mode value must not suppress the pipeline."""
    client, mock_faf = _make_client_and_mock(monkeypatch)

    with client:
        resp = client.post(
            "/api/v1/chat/simple",
            json={"message": "hi"},
            headers={EVAL_MODE_HEADER: "other-mode"},
        )

    assert resp.status_code == 200
    mock_faf.assert_called_once()


def test_eval_mode_constants_are_importable() -> None:
    """pa-eval must be able to import EVAL_MODE_HEADER and EVAL_MODE_SWEEP
    from compass_backend.api.chat to avoid string drift."""
    assert EVAL_MODE_HEADER == "x-compass-eval-mode"
    assert EVAL_MODE_SWEEP == "sweep"
