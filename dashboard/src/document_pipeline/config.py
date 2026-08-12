"""Pipeline configuration via environment variables (DOCPIPE_ prefix)."""

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """All settings for the document pipeline."""

    model_config = SettingsConfigDict(
        env_prefix="DOCPIPE_", env_file=".env", extra="ignore", populate_by_name=True,
    )

    # Execution
    dry_run: bool = Field(default=True, description="Preview without writing to DB")
    limit: int = Field(default=0, ge=0, description="Max documents to process (0 = no limit)")
    district_id: int | None = Field(default=None, description="Filter to one district")
    process_all: bool = Field(default=False, description="Process all districts")

    # Extraction
    http_timeout: int = Field(default=60, ge=10, description="Download timeout in seconds")
    use_ocr: bool = Field(default=True, description="Enable OCR for scanned PDFs")

    # Enrichment
    google_api_key: str | None = Field(
        default=None, description="Gemini API key",
        validation_alias=AliasChoices("DOCPIPE_GOOGLE_API_KEY", "GOOGLE_API_KEY"),
    )
    enrichment_model: str = Field(default="gemini-2.5-flash", description="Model for AI enrichment")
    max_text_for_enrichment: int = Field(default=50000, description="Max chars sent to Gemini")

    # Vespa Cloud
    vespa_url: str | None = Field(default=None, description="Vespa Cloud endpoint URL")
    vespa_cert_path: str | None = Field(default=None, description="mTLS certificate path")
    vespa_key_path: str | None = Field(default=None, description="mTLS private key path")

    # Embedding
    embed_model: str = Field(default="gemini-embedding-001", description="Embedding model")
    embed_dim: int = Field(default=768, description="Embedding dimensionality (Matryoshka)")
    embed_batch_size: int = Field(default=100, ge=1, le=100, description="Texts per API call")
    embed_max_concurrent: int = Field(default=8, ge=1, le=32, description="Concurrent embed batches")

    # Summarization
    summary_model: str = Field(default="gemini-2.5-flash-lite", description="Chunk summary model")
    summary_max_concurrent: int = Field(default=16, ge=1, le=64, description="Concurrent summary calls")

    # PostgreSQL — checks DOCPIPE_PG_* first, falls back to shared PG_*
    pg_host: str = Field(
        default="localhost",
        validation_alias=AliasChoices("DOCPIPE_PG_HOST", "PG_HOST"),
    )
    pg_port: int = Field(
        default=5432,
        validation_alias=AliasChoices("DOCPIPE_PG_PORT", "PG_PORT"),
    )
    pg_database: str = Field(
        default="postgres",
        validation_alias=AliasChoices("DOCPIPE_PG_DATABASE", "PG_DATABASE"),
    )
    pg_user: str = Field(
        default="postgres",
        validation_alias=AliasChoices("DOCPIPE_PG_USER", "PG_USER"),
    )
    pg_password: str = Field(
        default="",
        validation_alias=AliasChoices("DOCPIPE_PG_PASSWORD", "PG_PASSWORD"),
    )
