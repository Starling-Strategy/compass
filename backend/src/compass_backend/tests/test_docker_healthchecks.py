"""Regression tests for API container healthcheck targets."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_api_docker_healthcheck_uses_liveness_probe() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile.api").read_text(encoding="utf-8")

    assert "CMD curl -f http://localhost:8000/api/v1/health || exit 1" in dockerfile
    assert "CMD curl -f http://localhost:8000/api/v1/ready || exit 1" not in dockerfile


def test_fresh_api_docker_healthcheck_does_not_require_database_readiness() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile.compass_api_fresh").read_text(
        encoding="utf-8"
    )

    assert "CMD curl -f http://localhost:8000/api/v1/health || exit 1" in dockerfile
    assert "data.get('database') == 'connected'" not in dockerfile
