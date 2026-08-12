"""Configuration for the fresh Compass API shell."""

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# VENDORED-COPY PATCH (dashboard repo only): the canonical config.py imports
# AnswerLayerMode from compass_backend.contracts.answer_layer and derives the
# current_academic_year from compass_backend.planning.temporal. Neither of
# those packages is vendored into the standalone dashboard slice, so we
# inline the Literal alias and the year literal here instead. Re-apply
# after any sync from policy-advisor:main.

AnswerLayerMode = Literal["off", "shadow", "gated"]

_LOGFIRE_PLACEHOLDER_MARKERS = (
    "your_write_token_here",
    "your_",
    "placeholder",
    "changeme",
)


class Settings(BaseSettings):
    """Settings needed before deeper backend layers are admitted."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    host: str = Field(default="0.0.0.0", description="Server bind address")
    port: int = Field(default=8000, description="Server port")
    debug: bool = Field(default=False, description="Enable local reload mode")
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Runtime environment",
    )

    cors_origins: list[str] = Field(
        default=[
            "http://localhost:3000",
            "http://localhost:5001",
            "http://localhost:8000",
            "https://staging.nctq.ai",
            "https://nctq.ai",
            "https://compass.nctq.ai",
            "https://nctq.org",
            "https://www.nctq.org",
        ],
        description="Allowed browser origins for the API",
    )
    chat_message_max_chars: int = Field(
        default=2000,
        ge=1,
        description="Maximum accepted user message length for MVP chat.",
    )
    chat_rate_limit_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "COMPASS_CHAT_RATE_LIMIT_ENABLED",
            "chat_rate_limit_enabled",
        ),
        description=(
            "Throttle the cost-bearing chat routes per caller identity to cap "
            "financial-DoS via the LLM gateway. Admin callers are exempt so "
            "the pa-eval sweep harness is unaffected."
        ),
    )
    chat_rate_limit_per_minute: int = Field(
        default=60,
        ge=1,
        validation_alias=AliasChoices(
            "COMPASS_CHAT_RATE_LIMIT_PER_MINUTE",
            "chat_rate_limit_per_minute",
        ),
        description=(
            "Max /chat and /chat/simple requests per minute per identity "
            "(authenticated key/email, else client IP) before returning 429."
        ),
    )
    debug_report_rate_limit_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "COMPASS_DEBUG_REPORT_RATE_LIMIT_ENABLED",
            "debug_report_rate_limit_enabled",
        ),
        description=(
            "Throttle the UNAUTHENTICATED /api/v1/debug/report routes per "
            "client IP. The debug link is the only access control, so this "
            "caps storage-abuse floods from anyone who has a link (#1349)."
        ),
    )
    debug_report_rate_limit_per_minute: int = Field(
        default=20,
        ge=1,
        validation_alias=AliasChoices(
            "COMPASS_DEBUG_REPORT_RATE_LIMIT_PER_MINUTE",
            "debug_report_rate_limit_per_minute",
        ),
        description=(
            "Max /api/v1/debug/report requests per minute per client IP "
            "before returning 429. Conservative because reviewer submissions "
            "are infrequent and human-paced."
        ),
    )
    current_academic_year: str = Field(
        # VENDORED-COPY PATCH: literal default (canonical main derives this from
        # planning.temporal, which isn't vendored into the dashboard).
        default="2024 - 2025",
        description="Academic year used by deterministic data execution.",
    )
    session_store_backend: Literal["postgres", "memory"] = Field(
        default="postgres",
        description="Session persistence backend for the fresh chat route.",
    )
    api_key_auth_enabled: bool = Field(
        default=False,
        description="Require Compass API-key authentication for fresh chat routes.",
    )
    catalog_candidate_fusion_enabled: bool = Field(
        default=False,
        description=(
            "Feature flag: when True, CatalogResolver fetches a wider metric "
            "candidate pool and applies deterministic reciprocal-rank fusion. "
            "Off keeps existing metric candidate search behavior."
        ),
    )
    catalog_topic_narrowing_enabled: bool = Field(
        default=False,
        description=(
            "Feature flag: when True, CatalogResolver uses governed topic "
            "frames to prioritize metric candidates before adjudication. "
            "Off keeps existing metric resolution behavior."
        ),
    )
    catalog_recall_shadow_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "COMPASS_CATALOG_RECALL_SHADOW_ENABLED",
            "catalog_recall_shadow_enabled",
        ),
        description=(
            "Collect advisory catalog-recall reports for trace/debug metadata "
            "without changing planner or execution authority. Enabled by "
            "default so missed recognition is visible while Compass is built."
        ),
    )
    catalog_resolver_recall_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "COMPASS_CATALOG_RESOLVER_RECALL_ENABLED",
            "catalog_resolver_recall_enabled",
        ),
        description=(
            "When True, CatalogResolver gathers supported candidate lists "
            "through CatalogRecallService while retaining final approval/refusal "
            "authority."
        ),
    )
    answer_layer_mode: AnswerLayerMode = Field(
        default="gated",
        validation_alias=AliasChoices(
            "COMPASS_ANSWER_LAYER_MODE",
            "answer_layer_mode",
        ),
        description=(
            "Answer rewrite mode. Off preserves deterministic renderer output; "
            "shadow records an internal safety report; gated replaces the body "
            "when validation accepts the draft."
        ),
    )
    answer_layer_result_types: str = Field(
        default="metric_lookup,metric_ranking,metric_count,policy_guidance",
        validation_alias=AliasChoices(
            "COMPASS_ANSWER_LAYER_RESULT_TYPES",
            "answer_layer_result_types",
        ),
        description=(
            "Comma-separated ResponseManifest result_type values eligible for "
            "the optional guarded answer layer."
        ),
    )
    conversation_memory_result_max_bytes: int = Field(default=250_000, ge=0)

    selection_default_largest_limit: int = Field(
        default=10,
        ge=1,
        description=(
            "Default row count when the planner asks for the 'largest' "
            "districts without an explicit limit. Overridable per request "
            "via QueryPlan.limit; this is the fallback."
        ),
    )
    ranking_display_limit: int = Field(
        default=10,
        ge=1,
        description=(
            "Default preview row count for ranking results in the rendered "
            "answer. Underlying export always includes every row; this only "
            "caps the table preview."
        ),
    )

    pg_host: str = Field(default="localhost", description="PostgreSQL host")
    pg_port: int = Field(default=5432, description="PostgreSQL port")
    pg_database: str = Field(default="postgres", description="PostgreSQL database")
    pg_user: str = Field(default="postgres", description="PostgreSQL user")
    pg_password: SecretStr = Field(
        default=SecretStr(""),
        description="PostgreSQL password",
    )
    pg_schema: str = Field(
        default="compass",
        description="PostgreSQL schema for Compass tables and views.",
    )
    pg_command_timeout: float = Field(
        default=60.0,
        gt=0,
        description="asyncpg command timeout in seconds.",
    )
    pg_pool_min_size: int = Field(
        # Audit finding #1: the chat hot path used to open a fresh
        # asyncpg.connect() per query. The singleton pool wired by
        # create_chat_pool() warms `min_size` connections at boot so the
        # first turn after a deploy is not paying the handshake cost.
        # Tunable via env (PG_POOL_MIN_SIZE) to fit per-environment Postgres
        # ceilings without a code change.
        default=5,
        ge=0,
        description="Minimum connections held open by the chat asyncpg pool.",
    )
    pg_pool_max_size: int = Field(
        # `max_size` caps how many simultaneous chat-path queries can talk
        # to Postgres. 20 is a conservative default chosen to stay well
        # below the typical staging `max_connections=100` ceiling even with
        # several backend replicas. Operators should re-tune this against
        # the deployed Postgres `max_connections` before scaling up.
        default=20,
        ge=1,
        description="Maximum connections held open by the chat asyncpg pool.",
    )
    pg_pool_max_inactive_lifetime: float = Field(
        # Issue #1122: asyncpg has no pre-ping. An idle connection that gets
        # NAT-reclaimed upstream can be handed out dead, surfacing as an
        # intermittent 500 (live path) or a `ConnectionDoesNotExistError` /
        # connect-timeout (sweep path). Closing connections idle longer than
        # this lifetime forces a fresh, healthy connect instead of handing
        # out a reclaimed one. 300s matches asyncpg's own default; lower it
        # (env PG_POOL_MAX_INACTIVE_LIFETIME) when the upstream NAT idle
        # window is shorter than this default.
        default=300.0,
        gt=0,
        description=(
            "Seconds an asyncpg pool connection may sit idle before it is "
            "closed and recycled (guards against NAT-reclaimed stale "
            "connections)."
        ),
    )

    trusted_proxies: list[str] = Field(
        default=[],
        validation_alias=AliasChoices(
            "TRUSTED_PROXIES",
            "trusted_proxies",
        ),
        description=(
            "Reverse-proxy IPs/CIDRs whose X-Forwarded-For / X-Real-IP "
            "headers may be trusted for client identity. Empty default "
            "means 'no proxies trusted' — request.client.host is always "
            "used. Set TRUSTED_PROXIES=10.0.0.0/8,172.16.0.0/12 (etc.) in "
            "environments where Coolify/Traefik or another reverse proxy "
            "sits in front of the API. Audit finding #4."
        ),
    )

    logfire_token: SecretStr | None = Field(
        default=None,
        description="Logfire write token",
    )
    logfire_read_token: SecretStr | None = Field(
        default=None,
        description=(
            "Logfire read-scoped token for querying traces from the verdict "
            "pipeline (span evaluators). The write token cannot read."
        ),
    )
    logfire_project: str = Field(
        default="policy-advisor",
        description="Logfire project/service name",
    )

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, value: list[str], info) -> list[str]:
        """Reject wildcard browser access outside development."""

        environment = info.data.get("environment", "development")
        if environment != "development" and "*" in value:
            raise ValueError("Wildcard CORS is only allowed in development")
        return value

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _split_trusted_proxies(cls, value: object) -> object:
        """Accept either a JSON list or a comma-separated string.

        Coolify/Traefik deployments pass ``TRUSTED_PROXIES=10.0.0.0/8,...``
        as a plain env var; this validator keeps that ergonomic without
        forcing operators to JSON-encode the list.
        """

        if isinstance(value, str):
            return [entry.strip() for entry in value.split(",") if entry.strip()]
        return value

    @computed_field
    @property
    def auth_enabled(self) -> bool:
        """Whether any request authentication is configured."""

        return self.api_key_auth_enabled

    @computed_field
    @property
    def logfire_enabled(self) -> bool:
        """Whether Logfire should be configured on startup."""

        if self.logfire_token is None:
            return False
        token = self.logfire_token.get_secret_value().strip()
        if not token:
            return False
        lowered = token.lower()
        return not any(marker in lowered for marker in _LOGFIRE_PLACEHOLDER_MARKERS)


settings = Settings()
