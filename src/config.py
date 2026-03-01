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
    openai_api_key: str | None = Field(None, description="OpenAI API key")
    grok_api_key: str | None = Field(None, description="xAI API key for Grok models")
    default_model: str = Field(
        "claude-sonnet-4-6", description="Default LLM model for planning and ask"
    )
    enable_file_summaries: bool = Field(
        False, description="Whether to extract and index LLM file summaries"
    )
    summary_model: str = Field(
        "claude-haiku", description="Fast, cheap model for file summaries (default: claude-haiku)"
    )
    anthropic_model: str = Field(
        "claude-sonnet-4-6", description="Deprecated: use default_model instead"
    )
    planning_context_budget: int = Field(
        10000, description="Base token budget for planning context"
    )
    planning_max_output_tokens: int = Field(
        16000, description="Max output tokens for plan generation"
    )
    planning_thinking_budget: int = Field(
        10000, description="Token budget for extended thinking (0 to disable)"
    )
    planning_max_context_budget: int = Field(
        30000, description="Max token budget (used for complex multi-file queries)"
    )
    planning_candidate_base: int = Field(15, description="Base candidate count for hybrid search")
    planning_candidate_max: int = Field(
        40, description="Max candidates for complex queries on large codebases"
    )
    planning_rerank_base: int = Field(10, description="Base rerank top-N")
    planning_rerank_max: int = Field(25, description="Max rerank top-N for complex queries")
    planning_import_depth: int = Field(
        2, description="How many import hops to follow for dependency context"
    )

    # ── Webhook Auto-Registration ─────────────────────────────────────────────
    public_base_url: str | None = Field(
        None, description="Public URL of this server for GitHub webhooks"
    )

    @property
    def webhook_url(self) -> str | None:
        if not self.public_base_url:
            return None
        return self.public_base_url.rstrip("/") + "/webhook"

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

    supported_extensions: str = Field(
        ".py,.ts,.tsx,.js,.jsx,.java,.go,.rs,.cpp,.c,.cs,.rb,.swift,.kt,"
        ".json,.md,.yaml,.yml,.html,.css,.scss,.sh,.sql,.xml,.toml"
    )
    ignore_patterns: str = Field(
        "node_modules,.git,__pycache__,.venv,dist,build,.next,.min.js,"
        ".min.css,vendor/,fixtures/,__fixtures__,.yarn,.pnp,"
        ".cache,.turbo,.parcel-cache,"
        "__tests__,__mocks__,__snapshots__,.test.,_test.,.spec.,_spec.,"
        "test/,tests/,testing/,testdata/"
    )

    # ── Reranker ─────────────────────────────────────────────────────────────
    reranker_model: str = Field("cross-encoder/ms-marco-MiniLM-L-6-v2")
    reranker_top_n: int = Field(20, description="Candidates passed to reranker")

    # ── Retrieval ────────────────────────────────────────────────────────────
    retrieval_rrf_k: int = Field(60, description="RRF K constant")
    retrieval_candidate_multiplier: int = Field(
        4, description="Multiplier for candidates before RRF"
    )
    retrieval_keyword_tsvector_weight: float = Field(
        0.7, description="Weight for full text match in keyword search"
    )
    retrieval_keyword_trgm_weight: float = Field(
        0.3, description="Weight for trigram match in keyword search"
    )
    hnsw_ef_search: int = Field(
        40,
        description="HNSW ef_search parameter — higher = better recall, slower query (range: 10-200)",
    )

    # ── Custom Skills ─────────────────────────────────────────────────────────
    custom_skills_dirs: str = Field(
        "", description="Comma-separated paths to custom skill directories"
    )

    # ── Derived helpers ──────────────────────────────────────────────────────
    @property
    def custom_skills_dirs_list(self) -> list[str]:
        return [p.strip() for p in self.custom_skills_dirs.split(",") if p.strip()]

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
