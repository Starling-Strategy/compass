"""Tests for src/compass_data_sync/observability.py (Tier 2.6 audit).

Locks in the contract that batch-job processes now configure Logfire once at
startup, then use a span helper that no-ops cleanly when Logfire is disabled.
The 4 entry points (daily_sync, nces_import, sync_publications_airtable,
sync_navigator) all import these.
"""

from __future__ import annotations

import logfire.testing
import pytest

from compass_data_sync import observability


@pytest.fixture(autouse=True)
def _reset_observability_state() -> None:
    """Each test gets a clean module flag — prevents bleed from other tests."""
    observability._reset_for_tests()
    yield
    observability._reset_for_tests()


def test_setup_returns_false_without_logfire_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """setup() is a no-op when LOGFIRE_TOKEN is missing — batch jobs must still run."""
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    assert observability.setup(service_name="test-job") is False


def test_setup_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second setup() call returns True without re-configuring.

    Mocks logfire.configure / instrument_* to avoid mutating the global
    Logfire SDK state — important because that state leaks across tests
    and would point subsequent tests at a fake-token-rejecting endpoint.
    """
    import logfire

    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_v2_us_fake_token_for_idempotence_test")
    monkeypatch.setattr(logfire, "configure", lambda **kwargs: None)
    monkeypatch.setattr(logfire, "instrument_httpx", lambda: None)
    monkeypatch.setattr(logfire, "instrument_asyncpg", lambda: None)

    first = observability.setup(service_name="test-job")
    second = observability.setup(service_name="test-job")
    assert first is True
    assert second is True


def test_data_sync_span_is_noop_when_logfire_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """data_sync_span() yields a no-op context when setup() hasn't succeeded."""
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    observability.setup()  # returns False; flag stays unset

    # Should not raise, and the yielded span supports set_attribute.
    with observability.data_sync_span("test.span", foo="bar") as span:
        span.set_attribute("anything", "should-not-crash")


def test_data_sync_span_emits_real_span_when_configured(
    capfire: logfire.testing.CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the configured flag is set, data_sync_span yields a real Logfire span.

    capfire already configured Logfire with an in-memory TestExporter. We
    bypass setup() entirely (which would call logfire.configure() again and
    overwrite that exporter) and just flip the module flag, isolating the
    test to the helper's branch logic.
    """
    monkeypatch.setattr(observability, "_LOGFIRE_CONFIGURED", True)

    with observability.data_sync_span(
        "compass.data_sync.test",
        dry_run=True,
        category="alpha",
    ) as span:
        span.set_attribute("row_count", 42)

    spans = [
        s for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "compass.data_sync.test"
    ]
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["dry_run"] is True
    assert attrs["category"] == "alpha"
    assert attrs["row_count"] == 42
