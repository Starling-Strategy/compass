"""Settings regressions for catalog resolver feature flags."""

from __future__ import annotations

from pydantic import SecretStr

from compass_backend.config import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "pg_schema": "compass",
        "pg_password": SecretStr("test-password"),
    }
    base.update(overrides)
    return Settings(**base)


def test_catalog_topic_narrowing_flag_defaults_off() -> None:
    """Topic narrowing must default off so existing behavior is unchanged."""

    assert _settings().catalog_topic_narrowing_enabled is False


def test_catalog_topic_narrowing_flag_env_override(monkeypatch) -> None:
    """CATALOG_TOPIC_NARROWING_ENABLED env var enables topic narrowing."""

    monkeypatch.setenv("CATALOG_TOPIC_NARROWING_ENABLED", "true")
    assert Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
    ).catalog_topic_narrowing_enabled is True


def test_catalog_recall_shadow_flag_defaults_on() -> None:
    """Catalog recall evidence is collected by default."""

    assert _settings().catalog_recall_shadow_enabled is True


def test_catalog_recall_shadow_flag_reads_env(monkeypatch) -> None:
    """COMPASS_CATALOG_RECALL_SHADOW_ENABLED can disable recall diagnostics."""

    monkeypatch.setenv("COMPASS_CATALOG_RECALL_SHADOW_ENABLED", "false")
    assert Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
    ).catalog_recall_shadow_enabled is False


def test_catalog_resolver_recall_flag_defaults_on() -> None:
    """Resolver recall is part of the default governed runtime."""

    assert _settings().catalog_resolver_recall_enabled is True


def test_catalog_resolver_recall_flag_reads_env(monkeypatch) -> None:
    """COMPASS_CATALOG_RESOLVER_RECALL_ENABLED can disable resolver recall."""

    monkeypatch.setenv("COMPASS_CATALOG_RESOLVER_RECALL_ENABLED", "false")
    assert Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
    ).catalog_resolver_recall_enabled is False


def test_conversation_memory_result_size_cap_default() -> None:
    """Full result artifact persistence has a conservative size cap."""

    settings = _settings()

    assert settings.conversation_memory_result_max_bytes == 250_000


def test_selection_default_largest_limit_default() -> None:
    """`largest` selections default to the top 10 districts when planner is silent."""

    assert _settings().selection_default_largest_limit == 10


def test_selection_default_largest_limit_env_override(monkeypatch) -> None:
    """`SELECTION_DEFAULT_LARGEST_LIMIT` overrides the default at process start."""

    monkeypatch.setenv("SELECTION_DEFAULT_LARGEST_LIMIT", "25")
    assert Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
    ).selection_default_largest_limit == 25


def test_ranking_display_limit_default() -> None:
    """Ranking preview shows 10 rows by default; export still includes all rows."""

    assert _settings().ranking_display_limit == 10


def test_ranking_display_limit_env_override(monkeypatch) -> None:
    """`RANKING_DISPLAY_LIMIT` overrides the rendering preview cap."""

    monkeypatch.setenv("RANKING_DISPLAY_LIMIT", "5")
    assert Settings(
        pg_schema="compass",
        pg_password=SecretStr("test-password"),
    ).ranking_display_limit == 5
