"""Dashboard configuration via environment variables (NCTQAI_ prefix)."""

import re

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings

_COMPASS_API_KEY_RE = re.compile(r"^(pa_[^_]+_)([0-9a-fA-F]{8})([0-9a-fA-F]{24})$")
_COMPASS_API_KEY_ID_RE = re.compile(r"^pa_[^_]+_[0-9a-fA-F]{8}$")


class Config(BaseSettings):
    """All settings for the NCTQ.ai dashboard."""

    model_config = {
        "env_prefix": "NCTQAI_", "env_file": ".env", "extra": "ignore", "populate_by_name": True,
    }

    # PostgreSQL — checks NCTQAI_PG_* first, falls back to shared PG_*
    pg_host: str = Field(
        default="localhost",
        validation_alias=AliasChoices("NCTQAI_PG_HOST", "PG_HOST"),
    )
    pg_port: int = Field(
        default=5432,
        validation_alias=AliasChoices("NCTQAI_PG_PORT", "PG_PORT"),
    )
    pg_database: str = Field(
        default="postgres",
        validation_alias=AliasChoices("NCTQAI_PG_DATABASE", "PG_DATABASE"),
    )
    pg_user: str = Field(
        default="postgres",
        validation_alias=AliasChoices("NCTQAI_PG_USER", "PG_USER"),
    )
    pg_password: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("NCTQAI_PG_PASSWORD", "PG_PASSWORD"),
    )

    # Server
    dry_run: bool = Field(default=True, description="Preview without writing to DB")
    port: int = Field(default=5001, description="Dashboard server port")
    debug: bool = Field(default=False, description="Enable debug mode")
    lessons_dir: str = Field(
        default="",
        description="Override path to docs/lessons; empty = repo-root/docs/lessons",
    )
    environment: str = Field(
        default="development",
        validation_alias=AliasChoices("NCTQAI_ENV", "ENV"),
        description="Deployment environment (development, staging, production)",
    )

    # Auth
    auth_disabled: bool = Field(default=False, description="Disable auth for local development")
    session_secret: SecretStr = Field(
        default=SecretStr("change-me-in-production"),
        description="Secret for signing session cookies",
    )
    session_ttl_days: int = Field(default=7, description="Session expiration in days")
    otp_ttl_minutes: int = Field(default=10, description="OTP code expiration in minutes")
    otp_max_attempts: int = Field(default=3, description="Max wrong guesses per code")
    otp_rate_limit_per_email: int = Field(default=5, description="Max codes per email per hour")
    otp_rate_limit_per_ip: int = Field(default=10, description="Max failed verifications per IP per hour")

    # SMTP
    smtp_host: str = Field(default="", description="SMTP relay host")
    smtp_port: int = Field(default=587, description="SMTP relay port")
    smtp_user: str = Field(default="", description="SMTP username")
    smtp_password: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("NCTQAI_SMTP_PASSWORD", "RESEND_API"),
        description="SMTP password / Resend API key (NCTQAI_SMTP_PASSWORD, falls back to RESEND_API)",
    )
    smtp_from: str = Field(default="noreply@nctq.ai", description="From address for OTP emails")

    # Compass API
    compass_api_url: str = Field(
        default="http://localhost:8000",
        description="Base URL for the Compass API",
    )
    compass_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="API key for authenticating with the Compass API",
    )
    compass_cost_api_key_id_override: str = Field(
        default="",
        validation_alias=AliasChoices(
            "NCTQAI_COMPASS_COST_API_KEY_ID",
            "COMPASS_COST_API_KEY_ID",
        ),
        description="Safe Compass API key id to scope the Operations cost report",
    )
    compass_frontend_url: str = Field(
        default="http://localhost:3000",
        description="Base URL for the Compass chat frontend (launch links)",
    )
    compass_scenario_link_secret: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "NCTQAI_COMPASS_SCENARIO_LINK_SECRET",
            "COMPASS_SCENARIO_LINK_SECRET",
        ),
        description="Shared HMAC secret for signed Compass scenario debug links",
    )
    compass_scenario_link_ttl_seconds: int = Field(
        default=86400,
        ge=60,
        description="Lifetime for signed Compass scenario debug links",
    )

    # CSRF — allowed Origin/Referer hosts for non-HTMX cross-navigations.
    # Comma-separated, e.g. "https://staging.nctq.ai,https://nctq.ai".
    # Requests from these origins bypass the HX-Request requirement.
    allowed_origins: str = Field(
        default="http://localhost:5001,https://staging.nctq.ai,https://nctq.ai",
        description="CSRF Origin/Referer allowlist",
    )

    # Umami analytics (traffic monitor) — see SSN-180. Self-hosted Umami v2.20.2 lacks
    # user-managed API keys, so the dashboard authenticates via username/password and
    # caches a session JWT (see services/umami_stats.py). Empty username/password
    # disables the tile via Config.umami_enabled.
    umami_base_url: str = Field(
        default="https://umami.nctq.ai",
        description="Umami self-hosted base URL",
    )
    umami_username: str = Field(
        default="",
        description="Umami login username (empty = traffic monitor disabled)",
    )
    umami_password: SecretStr = Field(
        default=SecretStr(""),
        description="Umami login password (empty = traffic monitor disabled)",
    )
    umami_website_id: str = Field(
        default="90a06372-ed2b-42b1-b8a6-b0d922650521",
        description="Umami website UUID for the Compass frontend (provisioned 2026-04-30)",
    )

    @property
    def umami_enabled(self) -> bool:
        return bool(self.umami_username and self.umami_password.get_secret_value())

    @property
    def compass_api_key_id(self) -> str | None:
        """Safe display/reporting id for the configured Compass API key."""

        token = self.compass_api_key.get_secret_value().strip()
        if not token:
            return None
        if _COMPASS_API_KEY_ID_RE.fullmatch(token):
            return token
        match = _COMPASS_API_KEY_RE.fullmatch(token)
        if not match:
            return None
        return f"{match.group(1)}{match.group(2)}"

    @property
    def compass_cost_api_key_id(self) -> str | None:
        """Safe key id used to scope Compass cost reporting."""

        override = self.compass_cost_api_key_id_override.strip()
        if override:
            if _COMPASS_API_KEY_ID_RE.fullmatch(override):
                return override
            return None
        return self.compass_api_key_id
