"""Pytest configuration for NCTQ.ai dashboard tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]  # src/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def pytest_addoption(parser: pytest.Parser) -> None:
    # Guard against re-registration when this conftest is collected alongside
    # the compass_backend conftest, which also defines --run-live-db.
    try:
        parser.addoption(
            "--run-live-db",
            action="store_true",
            default=False,
            help="Run optional live staging-database smoke tests.",
        )
    except ValueError:
        pass  # already registered by another conftest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live_db: optional tests that require explicit --run-live-db and DB settings",
    )
