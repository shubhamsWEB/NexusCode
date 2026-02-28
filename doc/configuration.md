# Configuration Reference

All settings are read from environment variables or a `.env` file in the project root. The single source of truth is `src/config.py`.

---

## Required Variables

These must be set or the server will fail to start:

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string: `postgresql+asyncpg://user:pass@host:5432/db` |
| `VOYAGE_API_KEY` | Voyage AI API key — get one at [voyageai.com](https://www.voyageai.com) |
| `GITHUB_WEBHOOK_SECRET` | HMAC-SHA256 secret shared with GitHub |
| `JWT_SECRET` | Secret for signing auth tokens — min 32 chars |

---

## LLM Providers (at least one required for /plan and /ask)

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude models (claude-sonnet-4-6, claude-opus-4-6) |
| `OPENAI_API_KEY` | OpenAI models (gpt-4o, gpt-4o-mini, o3, o4-mini) |
| `GROK_API_KEY` | xAI models (grok-3, grok-3-mini) |
| `DEFAULT_MODEL` | Default LLM for /plan and /ask. Default: `claude-sonnet-4-6` |

---

## GitHub

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | — | Personal Access Token. Use OR App credentials. |
| `GITHUB_APP_ID` | — | GitHub App ID (integer) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | — | Path to GitHub App `.pem` private key file |
| `GITHUB_DEFAULT_BRANCH` | `main` | Branch to track for push events |
| `PUBLIC_BASE_URL` | — | Public URL for auto-registering webhooks, e.g. `https://nexuscode.yourcompany.com` |

---

## Database

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | required | `postgresql+asyncpg://...` |
| `DB_POOL_SIZE` | `10` | SQLAlchemy connection pool size |
| `DB_MAX_OVERFLOW` | `20` | Max overflow connections beyond pool |

---

## Redis

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection URL |

---

## Embeddings

| Variable | Default | Description |
|---|---|---|
| `VOYAGE_API_KEY` | required | Voyage AI API key |
| `EMBEDDING_MODEL` | `voyage-code-2` | Voyage model name |
| `EMBEDDING_DIMENSIONS` | `1536` | Vector dimensions (must match DB schema) |
| `EMBEDDING_BATCH_SIZE` | `128` | Max chunks per Voyage API call |

---

## Indexing & Chunking

| Variable | Default | Description |
|---|---|---|
| `CHUNK_TARGET_TOKENS` | `512` | Target chunk size in tokens |
| `CHUNK_OVERLAP_TOKENS` | `128` | Token overlap between consecutive chunks |
| `CHUNK_MIN_TOKENS` | `50` | Minimum chunk size (smaller chunks are merged) |
| `CONTEXT_TOKEN_BUDGET` | `8000` | Default token budget for assembled context |
| `SUPPORTED_EXTENSIONS` | `.py,.ts,...` | Comma-separated list of file extensions to index |
| `IGNORE_PATTERNS` | `node_modules,...` | Comma-separated glob patterns to skip |
| `ENABLE_FILE_SUMMARIES` | `false` | Extract LLM-generated file summaries during indexing |
| `SUMMARY_MODEL` | `claude-haiku` | Model used for file summaries |

---

## Retrieval

| Variable | Default | Description |
|---|---|---|
| `RETRIEVAL_RRF_K` | `60` | Reciprocal Rank Fusion constant |
| `RETRIEVAL_CANDIDATE_MULTIPLIER` | `4` | Candidates = top_k × multiplier before RRF |
| `RETRIEVAL_KEYWORD_TSVECTOR_WEIGHT` | `0.7` | Weight for tsvector full-text in keyword search |
| `RETRIEVAL_KEYWORD_TRGM_WEIGHT` | `0.3` | Weight for trigram symbol match in keyword search |
| `HNSW_EF_SEARCH` | `40` | HNSW search quality (10–200, higher = better recall, slower) |

---

## Reranker

| Variable | Default | Description |
|---|---|---|
| `RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Cross-encoder model (downloaded on first use, ~60MB) |
| `RERANKER_TOP_N` | `20` | Candidates passed to the reranker |

---

## Planning Mode

| Variable | Default | Description |
|---|---|---|
| `PLANNING_CONTEXT_BUDGET` | `10000` | Base token budget for planning context |
| `PLANNING_MAX_CONTEXT_BUDGET` | `30000` | Max budget for complex queries |
| `PLANNING_MAX_OUTPUT_TOKENS` | `16000` | Max tokens the planner LLM can output |
| `PLANNING_THINKING_BUDGET` | `10000` | Budget for Claude extended thinking (0 to disable) |
| `PLANNING_CANDIDATE_BASE` | `15` | Base candidate count for hybrid search |
| `PLANNING_CANDIDATE_MAX` | `40` | Max candidates for complex queries |
| `PLANNING_RERANK_BASE` | `10` | Base rerank top-N |
| `PLANNING_RERANK_MAX` | `25` | Max rerank top-N |
| `PLANNING_IMPORT_DEPTH` | `2` | Import hops to follow for dependency context |

---

## Auth (MCP)

| Variable | Default | Description |
|---|---|---|
| `JWT_SECRET` | required | HS256 signing secret for auth tokens |
| `JWT_EXPIRY_HOURS` | `8` | Token lifetime in hours |
| `GITHUB_OAUTH_CLIENT_ID` | — | GitHub OAuth App client ID (production OAuth flow) |
| `GITHUB_OAUTH_CLIENT_SECRET` | — | GitHub OAuth App client secret |

---

## Custom Skills

| Variable | Default | Description |
|---|---|---|
| `CUSTOM_SKILLS_DIRS` | `""` | Comma-separated directory paths containing SKILL.md files |

Examples:
```bash
CUSTOM_SKILLS_DIRS=/opt/team-skills
CUSTOM_SKILLS_DIRS=/opt/team-skills,/opt/shared-skills,./local-skills
```

---

## Example `.env` File

```bash
# ── Required ──────────────────────────────────────────────────────────
DATABASE_URL=postgresql+asyncpg://nexus:secret@localhost:5432/codebase_intel
VOYAGE_API_KEY=pa-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GITHUB_WEBHOOK_SECRET=openssl-rand-hex-32-output-here
JWT_SECRET=another-openssl-rand-hex-32-output

# ── GitHub ────────────────────────────────────────────────────────────
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# For GitHub App (alternative to GITHUB_TOKEN):
# GITHUB_APP_ID=123456
# GITHUB_APP_PRIVATE_KEY_PATH=./nexuscode.pem

# ── LLM ───────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# GROK_API_KEY=xai-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DEFAULT_MODEL=claude-sonnet-4-6

# ── Redis ─────────────────────────────────────────────────────────────
REDIS_URL=redis://localhost:6379

# ── Custom Skills ─────────────────────────────────────────────────────
CUSTOM_SKILLS_DIRS=/opt/my-team-skills

# ── Optional tuning ───────────────────────────────────────────────────
HNSW_EF_SEARCH=40
RERANKER_TOP_N=20
PLANNING_THINKING_BUDGET=10000
```

---

## Generating Secrets

```bash
# JWT_SECRET and GITHUB_WEBHOOK_SECRET — use a strong random value
openssl rand -hex 32

# Or with Python
python3 -c "import secrets; print(secrets.token_hex(32))"
```
