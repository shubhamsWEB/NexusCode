# Indexing Pipeline

The indexing pipeline converts raw GitHub source files into searchable, semantically-rich
code chunks stored in PostgreSQL. It runs asynchronously via RQ and is triggered by GitHub
push webhooks (incremental) or manual `/repos/{owner}/{name}/index` calls (full).

---

## Pipeline Stages

```
GitHub Repository
      │
      ▼ (webhook or manual trigger)
┌─────────────────────────────────┐
│  1. EVENT INTAKE                │
│  webhook.py / full_index.py     │
│  ● Verify HMAC-SHA256 sig       │
│  ● Parse push payload           │
│  ● Queue RQ job                 │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│  2. FILE FETCHING               │
│  github/fetcher.py              │
│  ● Fetch tree (changed files)   │
│  ● Merkle diff vs stored BLOBs  │
│  ● Batch-fetch file blobs       │
│  ● Max 10 concurrent requests   │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│  3. AST PARSING                 │
│  pipeline/parser.py             │
│  ● Tree-sitter parse tree       │
│  ● Extract symbols (functions,  │
│    classes, variables, imports) │
│  ● Detect language              │
│  ● Build scope chain            │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│  4. CHUNKING                    │
│  pipeline/chunker.py            │
│  ● Recursive split-then-merge   │
│  ● Target: 512 tokens           │
│  ● Overlap: 128 tokens          │
│  ● Min chunk: 50 tokens         │
│  ● Respects AST node boundaries │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│  5. ENRICHMENT                  │
│  pipeline/enricher.py           │
│  ● Prepend: file path           │
│  ● Prepend: language            │
│  ● Prepend: scope chain         │
│  ● Inject relevant imports      │
│  → enriched_content field       │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│  6. EMBEDDING                   │
│  pipeline/embedder.py           │
│  ● voyage-code-2 model          │
│  ● 1536-dimensional vectors     │
│  ● Batched (128 items/batch)    │
│  ● embeds enriched_content      │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│  7. UPSERT TO POSTGRESQL        │
│  storage/db.py                  │
│  ● upsert_chunks() (batch)      │
│  ● upsert_symbols() (batch)     │
│  ● update_merkle_nodes()        │
│  ● ON CONFLICT DO UPDATE        │
│    (restores soft-deleted rows) │
└─────────────────────────────────┘
```

---

## Stage Details

### 1. Event Intake

**Incremental (webhook):**
```
POST /webhook/github
  → Verify X-Hub-Signature-256 header (HMAC-SHA256 of body using GITHUB_WEBHOOK_SECRET)
  → Parse push event payload
  → Extract: repo, branch, before_sha, after_sha, commits[]
  → Queue: rq.Queue("indexing").enqueue(incremental_index, repo_owner, repo_name, ...)
  → Return 202 Accepted immediately
```

**Full index (manual):**
```
POST /repos/{owner}/{name}/index
  → Queue: rq.Queue("indexing").enqueue(full_index, owner, name, branch)
  → Fetches entire file tree from GitHub API
```

### 2. File Fetching & Merkle Diff

The Merkle tree approach avoids re-embedding unchanged files:

```python
# For each file in the push:
stored_sha = get_merkle_node(file_path, repo)   # from DB
github_sha = blob_sha from GitHub tree response  # from API

if stored_sha == github_sha:
    skip()   # file unchanged — no reprocessing needed
else:
    fetch_blob(file_path)   # download file content
    index_file(content)     # run through full pipeline
    update_merkle_node(file_path, new_sha)
```

GitHub blob SHAs are SHA-1 of `"blob {size}\0{content}"`. This gives us efficient change
detection without reading file content.

**Soft deletes for removed files:**
```python
if file was deleted in push:
    mark_chunks_deleted(file_path, repo)   # sets is_deleted = TRUE
    # chunks remain in DB for history; excluded from search
```

### 3. AST Parsing

Tree-sitter provides language-specific parse trees. The parser:
- Detects language from file extension
- Builds a traversal over the syntax tree
- Extracts structured symbols:

```python
Symbol:
  name:           str     # "authenticate"
  qualified_name: str     # "AuthService.authenticate"
  kind:           str     # function | class | method | variable | constant
  file_path:      str     # "src/auth/service.py"
  start_line:     int
  end_line:       int
  signature:      str     # "async def authenticate(self, token: str) -> User"
  docstring:      str     # first 500 chars of docstring/JSDoc
  scope_chain:    list    # ["AuthService", "authenticate"]
  imports:        list    # ["from fastapi import HTTPException", ...]
  is_exported:    bool
```

**Supported languages and parsers:**
| Language | Extension(s) | Parser |
|----------|-------------|--------|
| Python | .py | tree-sitter-python |
| TypeScript | .ts, .tsx | tree-sitter-typescript |
| JavaScript | .js, .jsx | tree-sitter-javascript |
| Java | .java | tree-sitter-java |
| Go | .go | tree-sitter-go |
| Rust | .rs | tree-sitter-rust |
| Others | various | graceful degradation |

### 4. Chunking Algorithm

```
Input: AST node tree for a file

1. Walk the AST, collect all top-level nodes (functions, classes, etc.)
2. For each node:
   a. If token_count(node) <= 512: use as-is → chunk
   b. If token_count(node) > 512: recursively split on child nodes
   c. If token_count(node) < 50: merge with adjacent node
3. Apply 128-token overlap between adjacent chunks:
   - Append last 128 tokens of chunk N to beginning of chunk N+1
4. Each chunk stores: start_line, end_line, symbol_name, symbol_kind, scope_chain

Result: 50–512 token chunks that:
  ● Never cut a function/class in the middle (AST-aware)
  ● Have overlap for context continuity
  ● Are associated with the symbol they belong to
```

**Token counting:** tiktoken `cl100k_base` (same tokenizer as Claude/GPT-4).

### 5. Enrichment

Before embedding, each chunk is enriched by prepending metadata:

```
# FILE: src/auth/service.py
# LANGUAGE: python
# SCOPE: AuthService > authenticate
# IMPORTS: from fastapi import HTTPException, from src.models import User

async def authenticate(self, token: str) -> User:
    ...
    (original chunk content follows)
```

This enrichment dramatically improves embedding quality because:
- The embedding model "sees" the file context even for a mid-file chunk
- Import context resolves type references that would otherwise be opaque
- Scope chain gives the embedding model class membership context

### 6. Embedding

```python
# voyage-code-2 specifics:
dimensions = 1536
model = "voyage-code-2"
input_type = "document"    # during indexing
                           # (vs "query" at search time for asymmetric retrieval)
batch_size = 128           # items per API call
rate_limit_handling = True # exponential backoff
```

**Asymmetric retrieval:** Voyage AI distinguishes between document embeddings (indexed content)
and query embeddings (search queries). This improves recall vs symmetric models.

### 7. PostgreSQL Upsert

```sql
INSERT INTO chunks (id, file_path, repo_owner, repo_name, language,
                    symbol_name, symbol_kind, scope_chain,
                    start_line, end_line, raw_content, enriched_content,
                    embedding, imports, token_count, commit_sha, commit_author, ...)
VALUES (...)
ON CONFLICT (id) DO UPDATE SET
    raw_content = EXCLUDED.raw_content,
    enriched_content = EXCLUDED.enriched_content,
    embedding = EXCLUDED.embedding,
    is_deleted = FALSE,   -- restore if previously soft-deleted
    indexed_at = NOW(),
    ...
```

The SHA-256 chunk ID is derived from `repo + file_path + start_line + end_line + content hash`,
ensuring stable IDs across re-indexing runs.

---

## Incremental vs Full Index

| | Incremental | Full Index |
|-|-------------|-----------|
| **Trigger** | GitHub push webhook | Manual API call |
| **Scope** | Changed files only (Merkle diff) | Entire repository tree |
| **Speed** | 2–4 seconds push-to-queryable | Minutes for large repos |
| **Use case** | Day-to-day operation | Initial setup, repair |
| **API** | Auto (webhook) | `POST /repos/{owner}/{name}/index` |

---

## Monitoring & Observability

**Check indexing status:**
```bash
# View recent webhook events
GET /events?limit=20

# Check per-repo stats
GET /stats/repos

# View recent indexed files
GET /stats/recent-files

# Check RQ job status
GET /jobs
```

**RQ Dashboard** (if enabled): `http://localhost:9181`

---

## Configuration Knobs

All in `src/config.py` / `.env`:

| Setting | Default | Effect |
|---------|---------|--------|
| `CHUNK_TARGET_TOKENS` | 512 | Target chunk size |
| `CHUNK_OVERLAP_TOKENS` | 128 | Overlap between chunks |
| `CHUNK_MIN_TOKENS` | 50 | Minimum chunk size before merging |
| `EMBED_BATCH_SIZE` | 128 | Voyage AI batch size |
| `INDEXING_EXTENSIONS` | 40+ types | File types to index |
| `INDEXING_IGNORE_PATTERNS` | node_modules, .git, etc. | Directories to skip |
| `GITHUB_DEFAULT_BRANCH` | main | Branch to index |
