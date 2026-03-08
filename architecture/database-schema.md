# Database Schema

NexusCode uses PostgreSQL 15+ with the `pgvector` extension. All tables use async SQLAlchemy 2.0.

---

## Schema Diagram

```
repos ──────────────────────────────────────────────────────────────┐
  │                                                                  │
  ├── chunks (main search index, HNSW vector, tsvector)             │
  ├── symbols (function/class definitions)                           │
  ├── merkle_nodes (file change detection)                           │
  └── webhook_events (delivery log)                                  │
                                                                     │
chat_sessions ──► chat_turns                                         │
plan_history_entries                                                 │
external_mcp_servers                                                 │
agent_role_overrides                                                 │
                                                                     │
workflow_definitions ──► workflow_runs ──► workflow_step_executions  │
                                       ──► human_checkpoints         │
                                       ──► generated_documents ◄─────┘
                                                                     │
knowledge_graph_edges                                                │
```

---

## Core Tables

### `chunks` — The Heart of Search

The primary indexed unit. Each row is a code fragment (50–512 tokens).

```sql
CREATE TABLE chunks (
    id              TEXT PRIMARY KEY,           -- SHA-256 of repo+path+lines+content
    file_path       TEXT NOT NULL,
    repo_owner      TEXT NOT NULL,
    repo_name       TEXT NOT NULL,
    commit_sha      TEXT,
    commit_author   TEXT,
    language        TEXT,                       -- python, typescript, go, etc.
    symbol_name     TEXT,                       -- function/class this chunk belongs to
    symbol_kind     TEXT,                       -- function, class, method, variable
    scope_chain     TEXT[],                     -- ["ClassName", "method_name"]
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    raw_content     TEXT NOT NULL,              -- original source code
    enriched_content TEXT,                     -- with prepended metadata
    embedding       vector(1536),              -- voyage-code-2 embedding
    imports         TEXT[],                     -- import statements in this file
    token_count     INTEGER,
    search_vector   tsvector,                   -- for keyword search
    is_deleted      BOOLEAN DEFAULT FALSE,      -- soft delete
    indexed_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_chunks_repo ON chunks(repo_owner, repo_name);
CREATE INDEX idx_chunks_file ON chunks(file_path, repo_owner, repo_name);
CREATE INDEX idx_chunks_language ON chunks(language);
CREATE INDEX idx_chunks_deleted ON chunks(is_deleted);
CREATE INDEX idx_chunks_search_vector ON chunks USING GIN(search_vector);

-- HNSW vector index (Migration 007)
CREATE INDEX idx_chunks_embedding_hnsw ON chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### `symbols` — Code Structure Index

All named symbols extracted by Tree-sitter AST parser.

```sql
CREATE TABLE symbols (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    qualified_name  TEXT NOT NULL,              -- "ClassName.method_name"
    kind            TEXT NOT NULL,              -- function|class|method|variable|constant|interface
    file_path       TEXT NOT NULL,
    repo_owner      TEXT NOT NULL,
    repo_name       TEXT NOT NULL,
    start_line      INTEGER,
    end_line        INTEGER,
    signature       TEXT,                       -- full function/class signature
    docstring       TEXT,                       -- first 500 chars of docstring
    scope_chain     TEXT[],
    is_exported     BOOLEAN DEFAULT TRUE,
    indexed_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_symbols_qualified ON symbols(qualified_name, file_path, repo_owner, repo_name);
CREATE INDEX idx_symbols_name_trgm ON symbols USING GIN(name gin_trgm_ops);
CREATE INDEX idx_symbols_repo ON symbols(repo_owner, repo_name);
```

### `repos` — Repository Registry

```sql
CREATE TABLE repos (
    id              SERIAL PRIMARY KEY,
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    branch          TEXT DEFAULT 'main',
    status          TEXT DEFAULT 'pending',     -- pending|indexing|indexed|error
    webhook_id      INTEGER,                    -- GitHub webhook ID
    last_indexed    TIMESTAMPTZ,
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(owner, name)
);
```

### `merkle_nodes` — Change Detection

Tracks the GitHub blob SHA for each file. On push, compare stored vs new SHA to skip unchanged files.

```sql
CREATE TABLE merkle_nodes (
    id          SERIAL PRIMARY KEY,
    file_path   TEXT NOT NULL,
    repo_owner  TEXT NOT NULL,
    repo_name   TEXT NOT NULL,
    blob_sha    TEXT NOT NULL,              -- GitHub's SHA-1 blob hash
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(file_path, repo_owner, repo_name)
);
```

### `webhook_events` — Delivery Audit Log

```sql
CREATE TABLE webhook_events (
    id              SERIAL PRIMARY KEY,
    delivery_id     TEXT UNIQUE,
    event_type      TEXT,                   -- push, ping, etc.
    repo_owner      TEXT,
    repo_name       TEXT,
    commit_sha      TEXT,
    files_changed   INTEGER,
    status          TEXT DEFAULT 'queued',  -- queued|processing|done|error
    error_message   TEXT,
    received_at     TIMESTAMPTZ DEFAULT NOW(),
    processed_at    TIMESTAMPTZ
);
```

---

## Conversation History

### `chat_sessions`

```sql
CREATE TABLE IF NOT EXISTS chat_sessions (
    id          TEXT PRIMARY KEY,               -- UUID4
    repo_owner  TEXT,
    repo_name   TEXT,
    model       TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    last_active TIMESTAMPTZ DEFAULT NOW()
);
```

### `chat_turns`

```sql
CREATE TABLE IF NOT EXISTS chat_turns (
    id          SERIAL PRIMARY KEY,
    session_id  TEXT REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,                  -- user | assistant
    content     TEXT NOT NULL,
    cited_files TEXT[],
    tokens      INTEGER,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_chat_turns_session ON chat_turns(session_id, created_at);
```

### `plan_history_entries`

```sql
CREATE TABLE IF NOT EXISTS plan_history_entries (
    id           TEXT PRIMARY KEY,             -- UUID4 (plan_id)
    query        TEXT NOT NULL,
    repo_owner   TEXT,
    repo_name    TEXT,
    response_type TEXT,
    model        TEXT,
    plan_json    JSONB,                         -- full ImplementationPlan as JSON
    tokens_used  INTEGER,
    elapsed_ms   INTEGER,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
```

---

## External MCP Servers

```sql
CREATE TABLE IF NOT EXISTS external_mcp_servers (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    name            TEXT NOT NULL UNIQUE,
    url             TEXT NOT NULL,
    description     TEXT,
    auth_header     TEXT,                       -- "Authorization: Bearer ..."
    is_enabled      BOOLEAN DEFAULT TRUE,
    tool_count      INTEGER DEFAULT 0,
    last_seen_at    TIMESTAMPTZ,
    error_count     INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Agent Role Overrides

```sql
CREATE TABLE IF NOT EXISTS agent_role_overrides (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    name            TEXT NOT NULL UNIQUE,       -- role name (matches _ROLES key or new custom)
    system_prompt   TEXT NOT NULL,
    instructions    TEXT,                       -- appended to system_prompt
    default_tools   TEXT[],
    require_search  BOOLEAN DEFAULT TRUE,
    max_iterations  INTEGER DEFAULT 5,
    token_budget    INTEGER DEFAULT 80000,
    is_active       BOOLEAN DEFAULT TRUE,
    is_builtin      BOOLEAN DEFAULT FALSE,      -- TRUE for builtins that have been overridden
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Workflow Engine

### `workflow_definitions`

```sql
CREATE TABLE IF NOT EXISTS workflow_definitions (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    yaml_definition TEXT NOT NULL,
    trigger_type    TEXT DEFAULT 'manual',      -- webhook|schedule|manual|event
    is_active       BOOLEAN DEFAULT TRUE,
    version         INTEGER DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### `workflow_runs`

```sql
CREATE TABLE IF NOT EXISTS workflow_runs (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    workflow_id         TEXT REFERENCES workflow_definitions(id) ON DELETE CASCADE,
    workflow_name       TEXT NOT NULL,
    status              TEXT DEFAULT 'pending',
        -- pending|running|completed|failed|waiting_human|cancelled
    trigger_payload     JSONB DEFAULT '{}',
    context_snapshot    JSONB DEFAULT '{}',
    total_tokens_used   INTEGER DEFAULT 0,
    error_message       TEXT,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX idx_workflow_runs_workflow ON workflow_runs(workflow_id, started_at DESC);
CREATE INDEX idx_workflow_runs_status ON workflow_runs(status);
```

### `workflow_step_executions`

```sql
CREATE TABLE IF NOT EXISTS workflow_step_executions (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    run_id          TEXT REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_id         TEXT NOT NULL,
    step_type       TEXT,
    agent_role      TEXT,
    status          TEXT DEFAULT 'pending',
        -- pending|running|completed|failed|skipped
    output          JSONB,                      -- {"text": "...", "documents": [...]}
    tokens_used     INTEGER DEFAULT 0,
    error_message   TEXT,
    retry_count     INTEGER DEFAULT 0,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX idx_step_exec_run ON workflow_step_executions(run_id, step_id);
```

### `human_checkpoints`

```sql
CREATE TABLE IF NOT EXISTS human_checkpoints (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    run_id          TEXT REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_id         TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    options         TEXT[] DEFAULT '{}',
    response        TEXT,
    status          TEXT DEFAULT 'waiting',     -- waiting|answered|timed_out|skipped
    timeout_hours   INTEGER DEFAULT 24,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    answered_at     TIMESTAMPTZ
);
```

### `generated_documents`

Stores PDFs generated by the `generate_pdf` agent tool.

```sql
CREATE TABLE IF NOT EXISTS generated_documents (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    run_id      TEXT REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_id     TEXT,
    title       TEXT NOT NULL,
    filename    TEXT NOT NULL,
    pdf_bytes   BYTEA NOT NULL,                 -- raw PDF data
    size_bytes  INTEGER NOT NULL,
    metadata    JSONB DEFAULT '{}',             -- service, severity, environment, etc.
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_generated_documents_run_id ON generated_documents(run_id);
CREATE INDEX idx_generated_documents_step ON generated_documents(run_id, step_id);
```

---

## Knowledge Graph

```sql
CREATE TABLE IF NOT EXISTS knowledge_graph_edges (
    id          SERIAL PRIMARY KEY,
    repo_owner  TEXT NOT NULL,
    repo_name   TEXT NOT NULL,
    source_id   TEXT NOT NULL,              -- file path or "file:path"
    target_id   TEXT NOT NULL,
    edge_type   TEXT NOT NULL,              -- imports|defines|contains|calls
    confidence  FLOAT DEFAULT 1.0,
    built_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_kg_edges_repo ON knowledge_graph_edges(repo_owner, repo_name);
CREATE INDEX idx_kg_edges_source ON knowledge_graph_edges(source_id);
```

---

## Migrations

| Migration | File | What it adds |
|-----------|------|--------------|
| 001 | 001_init.sql | Core tables: chunks, symbols, repos, merkle_nodes, webhook_events |
| 003 | 003_chat_history.sql | chat_sessions, chat_turns, plan_history_entries |
| 007 | 007_hnsw.sql | HNSW index on chunks.embedding (replaces ivfflat) |
| 008 | 008_external_mcp_servers.sql | external_mcp_servers table |
| 009 | (workflows) | workflow_definitions, workflow_runs, workflow_step_executions, human_checkpoints |
| 012 | 012_agent_roles.sql | agent_role_overrides table |
| 013 | 013_generated_documents.sql | generated_documents table |

Run all migrations in order:
```bash
for f in src/storage/migrations/*.sql; do
    psql $DATABASE_URL -f $f
done
```

---

## Connection Pool Configuration

```python
# src/storage/db.py
engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,          # health-check connections before use
    pool_recycle=3600,           # recycle connections after 1 hour
    echo=False,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
```

---

## Performance Notes

- **HNSW vs IVFFlat:** HNSW was migrated to in migration 007 for better recall at low latency.
  IVFFlat requires training data (known at index build time); HNSW grows dynamically.
- **tsvector index:** GIN index on `search_vector` enables fast full-text search.
- **pg_trgm index:** GIN index on `symbols.name` enables fuzzy symbol name matching.
- **Soft deletes:** `is_deleted = TRUE` chunks are excluded from all queries but kept for
  history and potential recovery. A `VACUUM` policy can clean them periodically.
