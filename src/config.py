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
    openai_api_key: str | None = Field(None, description="OpenAI API key for web research")
    grok_api_key: str | None = Field(None, description="Grok API key")
    # ── Ollama (local) ────────────────────────────────────────────────────────
    ollama_base_url: str = Field(
        "http://localhost:11434",
        description="Ollama API base URL. Set to empty string to disable Ollama routing.",
    )
    ollama_models: str = Field(
        "",
        description=(
            "Comma-separated model names routed to local Ollama. "
            "Example: 'glm-4.6:cloud,llama3.2:latest'"
        ),
    )
    default_model: str = Field(
        "claude-haiku-4-5-20251001", description="Default Claude model for planning and ask"
    )
    enable_file_summaries: bool = Field(
        False, description="Whether to extract and index LLM file summaries"
    )
    # ── Token Budgeting ───────────────────────────────────────────────────────
    token_budgeting_enabled: bool = Field(
        True,
        description=(
            "Master switch for all token budget limits. "
            "When True (default), context assembly, agent loop Gate 2, and planning "
            "retriever all enforce their configured token budgets. "
            "When False, all budget caps are removed — the LLM receives the full "
            "ranked context it needs for maximum accuracy. "
            "Disable only if your LLM context window is large enough to absorb "
            "uncapped retrieval results (e.g. claude-3-7-sonnet with 200K context)."
        ),
    )

    # Agent loop settings
    ask_max_iterations: int = Field(5, description="Max retrieval iterations for Ask Mode")
    plan_max_iterations: int = Field(10, description="Max retrieval iterations for Plan Mode (complex queries)")
    agent_token_budget: int = Field(
        50_000, description="Max cumulative tokens across all tool results in an agent loop"
    )
    planning_thinking_budget: int = Field(
        0, description="Token budget for extended thinking in Plan Mode (0 to disable)"
    )
    planning_max_output_tokens: int = Field(
        8000, description="Max output tokens for plan generation"
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
    chunk_target_tokens: int = Field(
        768,
        description=(
            "Target tokens per chunk. 768 fits most functions whole and reduces "
            "truncation of large methods. Changing this requires re-indexing all repos."
        ),
    )
    chunk_overlap_tokens: int = Field(
        128,
        description="Overlap tokens between adjacent chunks for context continuity.",
    )
    chunk_min_tokens: int = Field(50)
    context_token_budget: int = Field(12000)

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
    reranker_top_n: int = Field(30, description="Candidates passed to reranker")
    reranker_content_chars: int = Field(
        4000,
        description=(
            "Max characters of raw_content passed to the cross-encoder per scoring window. "
            "Code at ~4 chars/token fills ~1000 tokens; the model truncates to its 512-token "
            "limit. Larger values ensure the tokenizer sees more relevant code. "
            "Was 1500 — increasing to 4000 reduces truncation of large functions."
        ),
    )
    reranker_max_windows: int = Field(
        2,
        description=(
            "Max sliding windows per chunk for large-chunk reranking. "
            "1 = legacy single window. 2 = score beginning + tail and take MAX. "
            "Fixes mis-ranking of large functions where the critical code is mid-body."
        ),
    )

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
        80,
        description="HNSW ef_search parameter — higher = better recall, slower query (range: 10-200)",
    )
    retrieval_exhaustive_top_k: int = Field(
        200,
        description=(
            "top_k used for the 'exhaustive' search quality tier in complex/cross-cutting "
            "planning queries. Higher = better recall at the cost of reranking latency."
        ),
    )
    retrieval_min_quality_score: float = Field(
        0.10,
        description=(
            "Minimum sigmoid-normalized rerank quality score (0.0-1.0) for a chunk to be "
            "included in assembled context once the budget is >50% consumed. "
            "Set to 0.0 to disable quality filtering."
        ),
    )
    retrieval_reformulate_threshold: float = Field(
        0.40,
        description=(
            "If the best reranked candidate has quality_score below this threshold, "
            "the retriever attempts a heuristic query reformulation and re-searches. "
            "Set to 0.0 to disable reformulation entirely."
        ),
    )

    # ── Query relevance gate ──────────────────────────────────────────────────
    query_relevance_threshold: float = Field(
        0.35,
        description=(
            "Default min cosine similarity for a query to be considered relevant. "
            "Used when no per-complexity threshold matches. "
            "Range: 0.0-1.0. Lower = more permissive, higher = stricter."
        ),
    )
    query_relevance_soft_threshold: float = Field(
        0.50,
        description=(
            "Cosine similarity above which the query is clearly relevant (skips ambiguous zone). "
            "Must be > query_relevance_threshold."
        ),
    )
    query_relevance_threshold_simple: float = Field(
        0.50,
        description=(
            "Stricter relevance threshold for short/simple queries (< 40 chars, < 5 words). "
            "Simple queries that match weakly are more likely to be off-topic."
        ),
    )
    query_relevance_threshold_moderate: float = Field(
        0.30,
        description="Relevance threshold for moderate-length queries (default zone).",
    )
    query_relevance_threshold_complex: float = Field(
        0.15,
        description=(
            "Looser relevance threshold for long/multi-part queries. "
            "Complex queries often use natural-language framing that scores lower "
            "against code embeddings even when the intent is clearly codebase-related."
        ),
    )
    query_relevance_enabled: bool = Field(
        True,
        description="Set false to disable the relevance gate entirely (e.g. for testing).",
    )
    query_relevance_mode: str = Field(
        "strict",
        description=(
            "Planner relevance gate behavior: 'strict' rejects out-of-scope queries, "
            "'warn' logs and continues, 'off' skips the gate."
        ),
    )
    web_research_timeout_s: int = Field(
        0,
        description=(
            "Max seconds allowed for planner web research before it is skipped. "
            "Set to 0 to disable the timeout."
        ),
    )
    web_research_max_chars: int = Field(
        0,
        description=(
            "Max characters of web research notes injected into planner prompts. "
            "Set to 0 to disable truncation."
        ),
    )
    web_research_selective_trigger: bool = Field(
        False,
        description=(
            "If true, planner web research only runs for complex or externally-oriented queries. "
            "If false, it preserves legacy behavior and runs whenever web_research is enabled."
        ),
    )
    plan_max_iterations_simple: int = Field(
        5,
        description="Max retrieval iterations for simple plan queries.",
    )
    plan_max_iterations_moderate: int = Field(
        7,
        description="Max retrieval iterations for moderate plan queries.",
    )
    ask_max_iterations_complex: int = Field(
        8,
        description="Max retrieval iterations for Ask Mode when query is detected as complex.",
    )
    agent_token_budget_simple: int = Field(
        50_000,
        description="Cumulative planner tool-result token budget for simple queries.",
    )
    agent_token_budget_moderate: int = Field(
        50_000,
        description="Cumulative planner tool-result token budget for moderate queries.",
    )

    github_api_concurrency: int = Field(
        10, description="Max concurrent GitHub API file fetch calls during indexing"
    )
    search_result_cache_ttl: int = Field(
        300, description="TTL in seconds for full search result cache in Redis"
    )

    # ── Module Coverage Enforcement ──────────────────────────────────────────
    coverage_enforcement_enabled: bool = Field(
        True,
        description=(
            "When True, after the primary rerank the retriever checks whether "
            "key modules/directories mentioned in the query are represented in the "
            "candidate set. Missing modules get a targeted search to fill the gap. "
            "Reduces the risk of missing entire subsystems."
        ),
    )

    # ── Answer Verification ───────────────────────────────────────────────────
    answer_verification_enabled: bool = Field(
        False,
        description=(
            "When True, run a lightweight second LLM call after each answer to "
            "rate grounding confidence and flag unsupported claims. "
            "Adds ~300-600ms latency. Off by default."
        ),
    )
    verification_model: str = Field(
        "",
        description=(
            "Model to use for the verification pass. "
            "Defaults to default_model when empty. "
            "Recommended: a fast/cheap model like claude-haiku-4-5-20251001."
        ),
    )

    # ── Custom Skills ─────────────────────────────────────────────────────────
    custom_skills_dirs: str = Field(
        "", description="Comma-separated paths to custom skill directories"
    )

    # ── Cross-Repo Routing ────────────────────────────────────────────────────
    cross_repo_enabled: bool = Field(True, description="Enable intelligent cross-repo search routing")
    cross_repo_max_repos: int = Field(5, description="Max repos searched per query within scope")
    cross_repo_min_score: float = Field(0.20, description="Min combined score to include a repo")
    cross_repo_keyword_weight: float = Field(0.25, description="Weight of keyword Jaccard in combined score")
    cross_repo_semantic_weight: float = Field(0.75, description="Weight of centroid cosine in combined score")
    cross_repo_primary_budget_fraction: float = Field(0.60, description="Fraction of token budget for primary repo")
    cross_repo_router_cache_ttl: int = Field(120, description="Redis TTL for router summaries cache (seconds)")
    cross_repo_summary_update_min_chunks: int = Field(10, description="Min chunks before centroid is computed")

    # ── MCP OAuth ────────────────────────────────────────────────────────────
    mcp_oauth_callback_base_url: str = Field(
        "http://localhost:8000",
        description="Public base URL for MCP OAuth callback (e.g. https://nexus.myco.com)",
    )

    # ── API Key Scoping ───────────────────────────────────────────────────────
    api_key_header: str = Field("X-Api-Key", description="HTTP header name for API key")
    api_key_query_param: str = Field("api_key", description="URL query param fallback for MCP SSE URL")
    api_key_cache_ttl: int = Field(300, description="Redis TTL for key→scope cache (seconds)")

    # ── Workflow Automation Engine ────────────────────────────────────────────
    workflow_max_step_retries: int = Field(
        2, description="Default max retries per workflow step"
    )
    workflow_step_timeout_seconds: int = Field(
        300, description="Max seconds a single agent step may run"
    )
    workflow_human_checkpoint_timeout_hours: int = Field(
        24, description="Default hours before a human checkpoint times out"
    )
    workflow_max_parallel_steps: int = Field(
        4, description="Max steps that can run in parallel within a single wave"
    )

    # ── Agent Session / Artifact Store ───────────────────────────────────────
    agent_session_enabled: bool = Field(
        True, description="Enable Redis artifact store for agent loop"
    )
    artifact_ttl_seconds: int = Field(
        1800, description="TTL for artifact keys in Redis (seconds)"
    )
    artifact_summary_max_tokens: int = Field(
        200, description="Max tokens in a compressed artifact summary"
    )
    artifact_working_memory_prefix: str = Field(
        "session:wm", description="Redis key prefix for working memory"
    )

    # ── LangSmith Observability ───────────────────────────────────────────────
    langsmith_tracing: bool = Field(
        False, description="Enable LangSmith tracing for all LangGraph workflows"
    )
    langsmith_api_key: str | None = Field(None, description="LangSmith API key")
    langsmith_project: str = Field(
        "nexuscode-enterprise", description="LangSmith project name for trace grouping"
    )
    langsmith_endpoint: str = Field(
        "https://api.smith.langchain.com", description="LangSmith API endpoint"
    )

    # ── Enterprise Integration Gateway ────────────────────────────────────────
    integration_encryption_key: str | None = Field(
        None,
        description=(
            "32-byte hex key for AES-256-GCM encryption of integration credentials. "
            "Generate with: python -c \"import secrets; print(secrets.token_hex(32))\""
        ),
    )
    # Jira
    jira_base_url: str | None = Field(None, description="Jira instance base URL (e.g. https://myco.atlassian.net)")
    jira_api_token: str | None = Field(None, description="Jira API token for service account")
    jira_email: str | None = Field(None, description="Service account email for Jira Basic Auth")
    # Slack
    slack_bot_token: str | None = Field(None, description="Slack bot token (xoxb-...)")
    slack_signing_secret: str | None = Field(None, description="Slack signing secret for event verification")
    # GitHub (enterprise integrations beyond indexing)
    github_app_installation_id: int | None = Field(None, description="GitHub App installation ID")
    # Figma
    figma_access_token: str | None = Field(None, description="Figma personal access token or OAuth token")
    # Notion
    notion_api_key: str | None = Field(None, description="Notion integration token")
    # OAuth redirect base
    integration_oauth_callback_base_url: str = Field(
        "http://localhost:8000",
        description="Public base URL for integration OAuth callbacks",
    )

    # ── Self-Evolution ────────────────────────────────────────────────────────
    evolution_enabled: bool = Field(
        True, description="Enable automatic self-evolution cycles"
    )
    evolution_cycle_interval_hours: int = Field(
        24, description="Run a reflection cycle every N hours (per repo)"
    )
    evolution_min_interactions_to_reflect: int = Field(
        50, description="Min interactions since last cycle before auto-triggering a new one"
    )
    evolution_worldview_update_on_index: bool = Field(
        True, description="Regenerate the repo worldview after every successful index"
    )
    evolution_ab_test_sample_fraction: float = Field(
        0.10, description="Fraction of interactions assigned to A/B treatment group (0.0–1.0)"
    )
    evolution_max_param_change_pct: float = Field(
        20.0, description="Max allowed % change per parameter in a single reflection cycle"
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

    @field_validator("query_relevance_mode")
    @classmethod
    def validate_query_relevance_mode(cls, v: str) -> str:
        value = v.lower().strip()
        if value not in {"strict", "warn", "off"}:
            raise ValueError("QUERY_RELEVANCE_MODE must be one of: strict, warn, off")
        return value


# Module-level singleton — import this everywhere
settings = Settings()
