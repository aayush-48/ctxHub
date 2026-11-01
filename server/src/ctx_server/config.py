"""Runtime configuration, read from environment variables (prefix CTX_)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fixed by the database schema (state_items.embedding is vector(768)).
EMBEDDING_DIM = 768


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CTX_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    public_base_url: str = "http://localhost:8080"
    database_url: str = "postgresql+asyncpg://ctx:ctx@localhost:5432/ctx"
    db_pool_size: int = 5
    run_migrations_on_start: bool = True

    # Secret used to sign login-session cookies. Must be set in prod.
    session_secret: str = "dev-insecure-session-secret"

    # Identity providers. A provider is enabled when its client id is set.
    google_client_id: str | None = None
    google_client_secret: str | None = None
    github_client_id: str | None = None
    github_client_secret: str | None = None
    # Dev-only login form that trusts any email. Refused when env == "prod".
    dev_login: bool = False

    access_token_ttl_s: int = 3600
    refresh_token_ttl_s: int = 30 * 24 * 3600

    # Providers
    embedding_provider: Literal["vertex", "gemini_api", "local", "fake"] = "vertex"
    embedding_model: str = "gemini-embedding-001"
    arbiter_provider: Literal["vertex_gemini", "vertex_claude", "gemini_api", "none", "fake"] = (
        "vertex_gemini"
    )
    arbiter_model: str = "gemini-3-flash-preview"
    # Newer Gemini models are served only from the global Vertex endpoint.
    arbiter_location: str = "global"
    arbiter_min_confidence: float = 0.7
    nli_enabled: bool = False
    nli_model: str = "cross-encoder/nli-deberta-v3-small"
    gemini_api_key: str | None = None
    anthropic_api_key: str | None = None  # enables exact Claude token counting

    gcp_project: str | None = None
    vertex_location: str = "us-central1"
    claude_vertex_location: str = "global"

    storage_provider: Literal["gcs", "db"] = "db"
    gcs_bucket: str | None = None
    tasks_provider: Literal["cloud_tasks", "inline"] = "inline"
    tasks_queue: str | None = None  # projects/<p>/locations/<l>/queues/<q>
    tasks_service_account: str | None = None  # SA email Cloud Tasks signs OIDC tokens as

    # Cleaning pipeline thresholds (cosine similarity on embeddings).
    dedupe_threshold: float = 0.95
    related_threshold: float = 0.75
    max_items_per_commit: int = 50
    max_subject_chars: int = 200
    max_content_chars: int = 4000
    proposal_ttl_s: int = 24 * 3600

    # Importance weights: typeWeight, recency, useCount, llmScore.
    w_type: float = 0.45
    w_recency: float = 0.2
    w_use: float = 0.1
    w_llm: float = 0.25
    recency_half_life_days: float = 14.0

    # Rendering
    render_safety_margin: float = 0.08
    claude_estimate_factor: float = 1.2  # applied to o200k counts when no exact counter

    # Abuse limits (requests per minute per user and bucket).
    rate_limit_per_min: int = 120
    rate_limit_heavy_per_min: int = 20
    max_upload_bytes: int = 50 * 1024 * 1024

    otel_enabled: bool = False
    log_level: str = "INFO"

    allowed_cors_origins: list[str] = Field(default_factory=list)

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.is_prod:
        if s.session_secret.startswith("dev-"):
            raise RuntimeError("CTX_SESSION_SECRET must be set in prod")
        if s.dev_login:
            raise RuntimeError("CTX_DEV_LOGIN cannot be enabled in prod")
    return s
