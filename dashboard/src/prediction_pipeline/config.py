"""Pipeline configuration via Pydantic Settings.

All settings read from PREDICTOR_* environment variables.

PiedPiper equivalent: PredictionConfig dataclass + os.getenv() calls.
We use Pydantic Settings with PREDICTOR_ prefix for validation and env binding.
"""
from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PREDICTOR_", env_file=".env", extra="ignore", populate_by_name=True,
    )
    # Execution
    dry_run: bool = Field(default=True, description="Preview without writing to DB")
    district_id: int | None = Field(default=None, description="Filter to one district")
    ay_id: int = Field(default=25, description="Academic year ID (25 = 2024-2025)")
    q_ids: list[int] | None = Field(default=None, description="Specific question IDs")
    limit: int = Field(default=0, ge=0, description="Max questions (0 = no limit)")

    # K-Diversity
    k_min: int = Field(default=2, ge=1, description="Min retrieval depth (k=2..k_max gives 15 runs at default)")
    k_max: int = Field(default=16, ge=1, le=20, description="Max retrieval depth")
    ina_threshold: int = Field(default=9, ge=1, description="INA consensus threshold (absolute count, 9/15 = 60% at default k)")

    # Model
    prediction_model: str = Field(default="gemini-2.5-flash-lite", description="Gemini model")
    prediction_temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    google_api_key: str | None = Field(
        default=None, description="Gemini API key",
        validation_alias=AliasChoices("PREDICTOR_GOOGLE_API_KEY", "GOOGLE_API_KEY"),
    )

    # Caching
    use_caching: bool = Field(default=False, description="Use Gemini context caching for k-diversity predictions")
    caching_ttl: int = Field(default=300, ge=60, description="Cache TTL in seconds")

    # Vespa
    vespa_url: str | None = Field(default=None, description="Vespa Cloud endpoint")
    vespa_cert_path: str | None = Field(default=None, description="mTLS cert path")
    vespa_key_path: str | None = Field(default=None, description="mTLS key path")
    vespa_api_key: str | None = Field(default=None, description="Vespa API key")

    # Curation
    curation_timeout: int = Field(default=120, ge=10, description="Timeout (seconds) per AI curation sub-batch")
    curation_max_source_chars: int = Field(default=8000, ge=1000, description="Max source text chars per citation in curation prompt")
    curation_batch_size: int = Field(default=3, ge=1, description="Max citations per AI curation sub-batch")

    # PostgreSQL — checks PREDICTOR_PG_* first, falls back to shared PG_*
    pg_host: str = Field(
        default="localhost",
        validation_alias=AliasChoices("PREDICTOR_PG_HOST", "PG_HOST"),
    )
    pg_port: int = Field(
        default=5432,
        validation_alias=AliasChoices("PREDICTOR_PG_PORT", "PG_PORT"),
    )
    pg_database: str = Field(
        default="postgres",
        validation_alias=AliasChoices("PREDICTOR_PG_DATABASE", "PG_DATABASE"),
    )
    pg_user: str = Field(
        default="postgres",
        validation_alias=AliasChoices("PREDICTOR_PG_USER", "PG_USER"),
    )
    pg_password: str = Field(
        default="",
        validation_alias=AliasChoices("PREDICTOR_PG_PASSWORD", "PG_PASSWORD"),
    )

    # Concurrency
    question_concurrency: int = Field(default=5, ge=1, le=50, description="Max questions processed concurrently (controls rate limit pressure)")

    # Identity
    model_version: str = Field(default="nkotb-test")
    source: str = Field(default="piedpiper")

    # Evaluation
    save_snapshots: bool = Field(default=True, description="Save eval snapshots to disk")
    snapshot_dir: str = Field(default="eval_snapshots", description="Snapshot directory")

    @model_validator(mode="after")
    def _validate_k_range(self):
        if self.k_min > self.k_max:
            raise ValueError(f"k_min ({self.k_min}) must be <= k_max ({self.k_max})")
        return self

