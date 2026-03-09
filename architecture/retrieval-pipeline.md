# Retrieval Pipeline

NexusCode uses a **multi-stage hybrid retrieval pipeline** that combines semantic vector search,
lexical keyword search, reciprocal rank fusion, cross-encoder reranking, and token-budget-aware
context assembly.

---

## Pipeline Overview

```
User Query  +  API Key Scope (optional)
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│  STAGE 0: REPO SCOPE GATE  (src/api/middleware.py)       │
│  ● Extract API key from X-Api-Key header / ?api_key=     │
│  ● Hash → Redis cache → DB lookup → allowed_repos list   │
│  ● No key: unrestricted (all repos)                      │
│  ● Invalid key: HTTP 401 immediately                     │
└─────────────────────────────┬────────────────────────────┘
                              │  allowed_repos list
                              ▼
┌──────────────────────────────────────────────────────────┐
│  STAGE 1: QUERY EMBEDDING                                │
│  ● voyage-code-2 (input_type="query")                    │
│  ● Redis cache: avoid re-embedding identical queries     │
│  ● 1536-dim vector                                       │
└─────────────────────────────┬────────────────────────────┘
                              │
              ┌───────────────┴──────────────┐
              │  repo=None                   │  repo specified
              ▼                              ▼
┌─────────────────────────┐   ┌─────────────────────────────┐
│  STAGE 1b: REPO ROUTER  │   │  SINGLE-REPO PATH           │
│  (repo_router.py)        │   │  Skip router; search the    │
│  ● Load repo_summaries  │   │  specified repo only        │
│    (Redis cached)        │   └──────────────┬──────────────┘
│  ● Filter to allowed    │                  │
│    repos (scope gate)    │                  │
│  ● Score each repo:     │                  │
│    0.75×cosine(centroid) │                  │
│  + 0.25×jaccard(keywords)│                  │
│  ● Keep top-N repos     │                  │
│  ● Allocate token budgets│                  │
└───────────┬─────────────┘                  │
            │  top-N repos + budgets          │
            └──────────────┬─────────────────┘
                           │
                  ┌────────┴──────────┐
                  │                   │
                  ▼                   ▼
┌─────────────────────┐   ┌─────────────────────────────┐
│  STAGE 2a: SEMANTIC │   │  STAGE 2b: KEYWORD SEARCH   │
│  HNSW cosine search │   │  tsvector full-text +       │
│  via pgvector       │   │  pg_trgm trigram fuzzy      │
│  (top-k candidates) │   │  (top-k candidates)         │
└──────────┬──────────┘   └──────────────┬──────────────┘
           │                             │
           └──────────────┬──────────────┘
                          ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 3: RRF MERGE (Reciprocal Rank Fusion)            │
│  score(d) = Σ 1 / (k + rank_in_list)                   │
│  k = 60 (prevents top-rank monopoly)                   │
│  Best chunks from both modalities surface together      │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 4: CROSS-ENCODER RERANKING (per-repo)            │
│  cross-encoder/ms-marco-MiniLM-L-6-v2 (local, CPU)     │
│  ● Scores every (query, chunk) pair                     │
│  ● Sigmoid-normalized → quality_score (0.0–1.0)        │
│  ● Top-N candidates kept                               │
│  ● Runs independently per repo (cross-repo mode)        │
└──────────────────────────┬──────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 5: CONTEXT ASSEMBLY                              │
│  assembler.py / assemble_multi_repo()                   │
│  ● Single-repo: sort by rerank score, deduplicate,      │
│    truncate to token_budget, format context block       │
│  ● Multi-repo: per-repo assembly with ╔REPO╗ headers,   │
│    ordered by highest-scored chunk, budgets respected   │
│  ● Emit: context_text, tokens_used, retrieval_log       │
└─────────────────────────────────────────────────────────┘
```

---

## Stage Details

### Stage 1: Query Embedding + Cache

```python
# Embedding is asymmetric:
#   Documents indexed with input_type="document"
#   Queries embedded with input_type="query"
# This improves recall for short queries matching long code chunks.

async def embed_query(query: str) -> list[float]:
    # Check Redis cache first (5-minute TTL)
    cached = await redis.get(f"embed:{hash(query)}")
    if cached:
        return cached

    vector = await voyage.embed([query], model="voyage-code-2", input_type="query")
    await redis.setex(f"embed:{hash(query)}", 300, vector)
    return vector
```

### Stage 2a: Semantic Search (HNSW)

```sql
-- HNSW cosine similarity search via pgvector
-- SET LOCAL hnsw.ef_search = 40 for recall/speed balance

SELECT id, file_path, repo_owner, repo_name, language,
       symbol_name, symbol_kind, start_line, end_line,
       raw_content, enriched_content, token_count,
       1 - (embedding <=> :query_vector) AS score
FROM chunks
WHERE is_deleted = FALSE
  AND repo_owner = :repo_owner        -- optional repo filter
  AND language = :language            -- optional language filter
ORDER BY embedding <=> :query_vector  -- cosine distance ascending
LIMIT :top_k * 2;                     -- fetch 2x for RRF headroom
```

**HNSW parameters:**
- `m = 16` — max connections per node (build-time)
- `ef_construction = 64` — build-time accuracy parameter
- `hnsw.ef_search = 40` — query-time recall parameter (configurable)
- Distance metric: `vector_cosine_ops`

### Stage 2b: Keyword Search

```sql
-- tsvector full-text search (exact + stemmed) combined with pg_trgm trigram fuzzy
SELECT id, ..., ts_rank_cd(search_vector, query) AS score
FROM chunks
WHERE is_deleted = FALSE
  AND (
    search_vector @@ plainto_tsquery('english', :query)
    OR raw_content ILIKE :pattern          -- fallback for short queries
  )
ORDER BY score DESC
LIMIT :top_k * 2;
```

Keyword search excels for:
- Exact identifier names (`JWTMiddleware`, `authenticate`)
- Error message strings (`ECONNREFUSED`, `401 Unauthorized`)
- File-path substrings (`webhook.py`, `src/api`)

### Stage 3: RRF Merge

Reciprocal Rank Fusion mathematically combines ranked lists:

```python
def rrf_merge(semantic_results, keyword_results, k=60):
    scores = defaultdict(float)

    for rank, chunk in enumerate(semantic_results, start=1):
        scores[chunk.id] += 1.0 / (k + rank)

    for rank, chunk in enumerate(keyword_results, start=1):
        scores[chunk.id] += 1.0 / (k + rank)

    # Merge unique chunks, sort by combined RRF score
    all_chunks = {c.id: c for c in semantic_results + keyword_results}
    return sorted(all_chunks.values(), key=lambda c: scores[c.id], reverse=True)
```

**Why k=60?** A higher k dampens the advantage of being ranked #1 in either list, giving
chunks ranked moderately well in both lists a chance to surface. The constant k=60 was
empirically shown by the original RRF paper (Cormack et al., 2009) to work well across
diverse retrieval tasks.

### Stage 4: Cross-Encoder Reranking

```python
# cross-encoder/ms-marco-MiniLM-L-6-v2 runs locally, no API calls
reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

def rerank(query: str, results: list[SearchResult], top_n: int = 5):
    pairs = [(query, r.raw_content[:512]) for r in results]
    scores = reranker.predict(pairs)   # returns raw logit scores

    for r, score in zip(results, scores):
        r.rerank_score = float(score)
        r.quality_score = 1.0 / (1.0 + math.exp(-score))   # sigmoid

    return sorted(results, key=lambda r: r.rerank_score, reverse=True)[:top_n]
```

**Why a cross-encoder instead of bi-encoder?** Cross-encoders attend jointly over (query,
document) pairs and capture fine-grained relevance signals that bi-encoders miss. They're
slower than bi-encoders but only run on the top ~20 candidates post-RRF, not the full corpus.

**Quality score:** The sigmoid-normalized rerank score (`quality_score`) is exposed to callers
as a 0–1 confidence indicator of how relevant a chunk is to the query.

### Stage 5: Context Assembly

```python
def assemble(results: list[SearchResult], token_budget: int, query: str) -> AssembledContext:
    included = []
    total_tokens = 0
    seen_ids = set()

    for chunk in sorted(results, key=lambda r: r.rerank_score, reverse=True):
        if chunk.id in seen_ids:
            continue    # deduplicate
        if total_tokens + chunk.token_count > token_budget:
            break       # respect token budget
        included.append(chunk)
        total_tokens += chunk.token_count
        seen_ids.add(chunk.id)

    context_text = format_context(included)  # readable, header-separated blocks
    return AssembledContext(context_text, total_tokens, retrieval_log=...)
```

**Context format per chunk:**
```
=== src/auth/service.py (lines 45–89) | python | AuthService.authenticate ===
Rerank score: 0.94 | Commit: a3f91bc

async def authenticate(self, token: str) -> User:
    """Validate JWT token and return the associated user."""
    ...
```

---

## Search Modes

| Mode | What runs | Best for |
|------|-----------|----------|
| `hybrid` (default) | Semantic + Keyword → RRF → Rerank | Most queries |
| `semantic` | Semantic only → Rerank | Conceptual/description queries |
| `keyword` | Keyword only | Exact identifiers, error strings, file paths |

---

## Search Quality Presets

The `search_quality` parameter controls `top_k` at the PostgreSQL level:

| Preset | Semantic candidates | Keyword candidates | Use case |
|--------|--------------------|--------------------|---------|
| `fast` | 10 | 10 | Quick lookups |
| `balanced` | 20 | 20 | Default for search API |
| `thorough` | 40 | 40 | Agent tool calls |
| `exhaustive` | 80 | 80 | Planning mode |

---

## 7-Phase Planning Retrieval

Planning Mode (`POST /plan`) runs an extended 7-phase retrieval pipeline:

```
Phase 1: Query embedding (+ cache check)
Phase 2: Semantic search (exhaustive preset, larger top_k)
Phase 3: Cross-encoder reranking
Phase 4: File structural maps (get_file_context for top files)
Phase 5: Caller graph extraction (find_callers for key symbols)
Phase 6: Web research (Anthropic web_search tool, optional)
Phase 7: Final context assembly (token-budget: 32K)
```

This gives the planning agent a much richer context than a standard search — it understands
not just what the code does, but how it connects to the rest of the codebase.

---

## HyDE (Hypothetical Document Embeddings)

Optional enhancement (configured via `RETRIEVAL_USE_HYDE=true`):

```
Query: "How does JWT token validation work?"
    │
    ▼ (LLM generates a hypothetical code snippet)
HyDE Doc: "async def validate_token(token: str) -> dict:
           payload = jwt.decode(token, SECRET, algorithms=['HS256'])
           ..."
    │
    ▼ (embed the hypothetical doc, not the original query)
Better semantic match against real code
```

HyDE improves recall for queries that are phrased as questions rather than code-like text.

---

## Relevance Gate

A pre-filter that prevents irrelevant queries from consuming LLM tokens:

```python
# Soft threshold: answer but warn user
SOFT_THRESHOLD = 0.35  (configurable)
# Hard threshold: refuse to answer
HARD_THRESHOLD = 0.20  (configurable)

# If max rerank score < threshold → mark as out_of_scope
# Exposed as: response_type="out_of_scope" in Ask/Plan responses
```

---

---

## Cross-Repo Routing (Stage 1b)

### Repo Summaries

Each repo has a pre-computed summary stored in `repo_summaries`:

```
repo_summaries row
├── centroid_embedding  vector(1536)   AVG of all chunk embeddings
├── tech_stack_keywords TEXT[]         top-50 frequent tokens from enriched_content
├── language_distribution JSONB        {"python": 0.72, "typescript": 0.28}
└── chunk_count         INTEGER        total indexed chunks (guards against noisy centroids)
```

Summaries are computed **post-indexing** (non-blocking `asyncio.create_task`) and cached in
Redis under `repo_router:summaries` (TTL 120s). The cache is invalidated after every
successful indexing job.

Repos with fewer than `cross_repo_summary_update_min_chunks` (default 10) are skipped —
their centroid would be unreliable.

### Scoring Algorithm

```python
# For each repo summary:
cos_sim  = cosine(query_vector, centroid_embedding)
jac_sim  = len(query_tokens ∩ tech_stack_keywords)
         / len(query_tokens ∪ tech_stack_keywords)

score = 0.75 × max(0, cos_sim) + 0.25 × jac_sim

# Filter, sort, take top-N
if score >= cross_repo_min_score:     # default 0.20
    include_repo(score)
```

Two signals reduce false positives:
- **Semantic similarity** catches repos by meaning even without exact keyword overlap
- **Keyword Jaccard** penalises repos with high centroid similarity but zero tech-stack overlap

### Budget Allocation

```python
floor = max(500, total_budget × 0.10)          # every repo gets at least this
remaining = total_budget − floor × len(repos)
budget[repo] = floor + remaining × (score / Σscores)
```

Even the lowest-scored included repo gets a meaningful context window.

### Scope Gate

The scope gate runs **before** cosine scoring:

```python
if allowed_repos:            # from API key
    summaries = [s for s in summaries
                 if f"{s.repo_owner}/{s.repo_name}" in allowed_set]
# Then score only the filtered list
```

This means out-of-scope repos are never scored, never searched, and never appear in results —
even if their centroid is highly similar to the query.

### `current_repo` Priority Hint

The caller can pass a `current_repo` hint (the repo the developer is actively working in).
That repo is always included first, with its score clamped to 1.0, regardless of centroid
similarity. This prevents the case where a developer queries from repo A and gets context
entirely from repo B.

---

## Repo Scope Enforcement in Tool Calls

All 7 internal tools enforce the `allowed_repos` scope using two centralised helpers in
`src/agent/tool_executor.py`:

```python
# For SQL-based lookups (get_symbol, get_file_context, get_agent_context):
repo_filter, params = _build_repo_scope_filter(repo_owner, repo_name, allowed_repos)
# → " AND (repo_owner || '/' || repo_name) = ANY(:scope_allowed_repos)"

# For in-memory result lists (find_callers semantic results, get_agent_context):
results = _filter_results_by_scope(results, repo_owner, repo_name, allowed_repos)
```

**Priority:** pinned repo > key scope > unrestricted

Any new tool added to the executor **must call these helpers** — the module docstring
documents this requirement explicitly.

---

## Configuration

| Setting | Default | Effect |
|---------|---------|--------|
| `HNSW_EF_SEARCH` | 40 | HNSW query-time recall (higher = slower but better) |
| `RRF_K` | 60 | RRF dampening constant |
| `RERANKER_TOP_N` | 5 | Max chunks after reranking |
| `SEARCH_TOP_K` | 8 | Default top_k for agent tool calls |
| `RETRIEVAL_TOKEN_BUDGET` | 8000 | Default context assembly budget |
| `RELEVANCE_SOFT_THRESHOLD` | 0.35 | Soft relevance gate |
| `RELEVANCE_HARD_THRESHOLD` | 0.20 | Hard relevance gate (refuse) |
| `CROSS_REPO_ENABLED` | `true` | Enable cross-repo routing |
| `CROSS_REPO_MAX_REPOS` | `5` | Max repos routed per query |
| `CROSS_REPO_MIN_SCORE` | `0.20` | Minimum combined score to include a repo |
| `CROSS_REPO_SEMANTIC_WEIGHT` | `0.75` | Weight of centroid cosine in combined score |
| `CROSS_REPO_KEYWORD_WEIGHT` | `0.25` | Weight of keyword Jaccard in combined score |
| `CROSS_REPO_ROUTER_CACHE_TTL` | `120` | Redis TTL for summaries cache (seconds) |
| `CROSS_REPO_SUMMARY_UPDATE_MIN_CHUNKS` | `10` | Min chunks before centroid is computed |
| `API_KEY_CACHE_TTL` | `300` | Redis TTL for key→scope lookup (seconds) |
