"""Tests for nctqai.config SecretStr discipline.

Covers audit finding #20: previously bare-`str` secret fields would print
verbatim in `repr(Config())` and pydantic validation errors. Switching to
`SecretStr` masks the value as `'**********'` until callers explicitly call
`.get_secret_value()`.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from nctqai.config import Config


SECRET_FIELDS = (
    "pg_password",
    "session_secret",
    "smtp_password",
    "compass_api_key",
    "compass_scenario_link_secret",
    "umami_password",
)


def test_secret_fields_are_secretstr() -> None:
    """Every plausibly-secret field on Config must be typed as SecretStr."""
    cfg = Config(
        pg_password="pg-shh",
        session_secret="session-shh",
        smtp_password="smtp-shh",
        compass_api_key="compass-shh",
        compass_scenario_link_secret="link-shh",
        umami_password="umami-shh",
    )
    for name in SECRET_FIELDS:
        value = getattr(cfg, name)
        assert isinstance(value, SecretStr), (
            f"Config.{name} must be SecretStr, got {type(value).__name__}"
        )


@pytest.mark.parametrize("field, plaintext", [
    ("pg_password", "pg-shh"),
    ("session_secret", "session-shh"),
    ("smtp_password", "smtp-shh"),
    ("compass_api_key", "compass-shh"),
    ("compass_scenario_link_secret", "link-shh"),
    ("umami_password", "umami-shh"),
])
def test_secret_fields_are_masked_in_repr(field: str, plaintext: str) -> None:
    """repr(Config()) must not leak any secret in plaintext."""
    cfg = Config(**{field: plaintext})
    text = repr(cfg)
    assert plaintext not in text, (
        f"Plaintext for {field} leaked through repr: {text}"
    )
    # Value should be retrievable via the documented escape hatch.
    assert getattr(cfg, field).get_secret_value() == plaintext


def test_umami_enabled_unwraps_password() -> None:
    """`umami_enabled` must use get_secret_value(); bool(SecretStr('')) is True."""
    disabled = Config(umami_username="", umami_password="")
    assert disabled.umami_enabled is False

    only_user = Config(umami_username="u", umami_password="")
    assert only_user.umami_enabled is False

    enabled = Config(umami_username="u", umami_password="pw")
    assert enabled.umami_enabled is True


def test_compass_api_key_id_derives_safe_display_id() -> None:
    cfg = Config(
        compass_api_key="pa_dev_c36eed70abcdefabcdefabcdefabcdef",
    )

    assert cfg.compass_api_key_id == "pa_dev_c36eed70"


def test_compass_api_key_id_accepts_existing_key_id() -> None:
    cfg = Config(compass_api_key="pa_dev_c36eed70")

    assert cfg.compass_api_key_id == "pa_dev_c36eed70"


def test_compass_api_key_id_ignores_unrecognized_secret_shape() -> None:
    cfg = Config(compass_api_key="not-a-compass-key")

    assert cfg.compass_api_key_id is None


def test_compass_cost_api_key_id_prefers_safe_override() -> None:
    cfg = Config(
        compass_api_key="pa_dev_c36eed70abcdefabcdefabcdefabcdef",
        compass_cost_api_key_id_override="pa_dev_c9d9244e",
    )

    assert cfg.compass_cost_api_key_id == "pa_dev_c9d9244e"


def test_compass_cost_api_key_id_falls_back_to_configured_token_id() -> None:
    cfg = Config(
        compass_api_key="pa_dev_c36eed70abcdefabcdefabcdefabcdef",
    )

    assert cfg.compass_cost_api_key_id == "pa_dev_c36eed70"


def test_compass_cost_api_key_id_rejects_unsafe_override() -> None:
    cfg = Config(
        compass_api_key="pa_dev_c36eed70abcdefabcdefabcdefabcdef",
        compass_cost_api_key_id_override="pa_dev_c9d9244eabcdefabcdefabcdefabcdef",
    )

    assert cfg.compass_cost_api_key_id is None


def test_smtp_password_falls_back_to_resend_api(monkeypatch):
    """smtp_password reads NCTQAI_SMTP_PASSWORD, else the existing RESEND_API.

    _env_file=None so the real .env (which has a live RESEND_API) is ignored.
    """
    monkeypatch.delenv("NCTQAI_SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("RESEND_API", "re_test_key_123")
    from nctqai.config import Config
    cfg = Config(_env_file=None)
    assert cfg.smtp_password.get_secret_value() == "re_test_key_123"


def test_smtp_password_prefers_explicit_override(monkeypatch):
    monkeypatch.setenv("NCTQAI_SMTP_PASSWORD", "explicit")
    monkeypatch.setenv("RESEND_API", "re_fallback")
    from nctqai.config import Config
    cfg = Config(_env_file=None)
    assert cfg.smtp_password.get_secret_value() == "explicit"
