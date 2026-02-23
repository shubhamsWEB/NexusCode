"""
Single source of truth for all configuration.
All values are read from environment variables (or .env file).
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── GitHub ──────────────────────────────────────────────────────────────
    # Use either a GitHub App OR a Personal Access Token.
    github_token: str | None = Field(None, description="Personal Access Token (quick start)")
    github_app_id: int | None = Field(None, description="GitHub App ID")
    github_app_private_key_path: str | None = Field(None, description="Path to .pem file")
    github_webhook_secret: str = Field(..., description="HMAC secret for webhook verification")
    github_default_branch: str = Field("main", description="Branch to track")

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = Field("postgresql+asyncpg://codebase:secret@localhost:5432/codebase_intel")
    db_pool_size: int = Field(10)
    db_max_overflow: int = Field(20)

    # ── Redis ────────────────────────────────────────────────────────────────
    redis_url: str = Field("redis://localhost:6379")

    # ── Embeddings ───────────────────────────────────────────────────────────
    voyage_api_key: str = Field(..., description="Voyage AI API key")
    embedding_model: str = Field("voyage-code-2")
    embedding_dimensions: int = Field(1536)
    embedding_batch_size: int = Field(128, description="Max chunks per Voyage API call")

    # ── LLM / Planning ───────────────────────────────────────────────────────
    anthropic_api_key: str | None = Field(None)
    anthropic_model: str = Field("claude-sonnet-4-6", description="Claude model for planning")
    planning_context_budget: int = Field(10000, description="Token budget for planning context")
    planning_max_output_tokens: int = Field(
        8000, description="Max output tokens for plan generation"
    )

    # ── MCP Auth ─────────────────────────────────────────────────────────────
    jwt_secret: str = Field(..., description="Secret for signing internal JWTs")
    jwt_expiry_hours: int = Field(8)
    github_oauth_client_id: str | None = Field(None)
    github_oauth_client_secret: str | None = Field(None)

    # ── Indexing ─────────────────────────────────────────────────────────────
    chunk_target_tokens: int = Field(512)
    chunk_overlap_tokens: int = Field(128)
    chunk_min_tokens: int = Field(50)
    context_token_budget: int = Field(8000)

    supported_extensions: str = Field(".py,.ts,.tsx,.js,.java,.go,.rs,.cpp,.c,.cs,.rb,.swift,.kt")
    ignore_patterns: str = Field("node_modules,.git,__pycache__,.venv,dist,build,.next,*.min.js")

    # ── Reranker ─────────────────────────────────────────────────────────────
    reranker_model: str = Field("cross-encoder/ms-marco-MiniLM-L-6-v2")
    reranker_top_n: int = Field(20, description="Candidates passed to reranker")

    # ── Derived helpers ──────────────────────────────────────────────────────
    @property
    def supported_extensions_set(self) -> set[str]:
        return {ext.strip() for ext in self.supported_extensions.split(",")}

    @property
    def ignore_patterns_list(self) -> list[str]:
        return [p.strip() for p in self.ignore_patterns.split(",")]

    @field_validator("database_url")
    @classmethod
    def validate_db_url(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection string")
        return v


# Module-level singleton — import this everywhere
settings = Settings()
