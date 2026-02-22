-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================
-- TABLE: chunks — Primary semantic retrieval table
-- ============================================================
CREATE TABLE IF NOT EXISTS chunks (
    -- Identity
    id               TEXT PRIMARY KEY,          -- SHA-256(enriched_content)
    file_path        TEXT NOT NULL,             -- "src/auth/service.py"
    repo_owner       TEXT NOT NULL,             -- "myorg"
    repo_name        TEXT NOT NULL,             -- "my-backend"
    commit_sha       TEXT NOT NULL,             -- git commit at time of indexing
    commit_author    TEXT,
    commit_message   TEXT,

    -- Code structure
    language         TEXT NOT NULL,             -- "python", "typescript", etc.
    symbol_name      TEXT,                      -- "UserService.authenticate"
    symbol_kind      TEXT,                      -- "function","class","method","module"
    scope_chain      TEXT,                      -- "UserService > authenticate"
    start_line       INTEGER NOT NULL,
    end_line         INTEGER NOT NULL,

    -- Content
    raw_content      TEXT NOT NULL,             -- original source code
    enriched_content TEXT NOT NULL,             -- scope-injected text (what was embedded)
    imports          TEXT[],
    token_count      INTEGER,

    -- Vector (voyage-code-2 = 1024 dims)
    embedding        vector(1536),           -- voyage-code-2 actual dimensions

    -- Lifecycle
    indexed_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    is_deleted       BOOLEAN DEFAULT FALSE
);

-- Vector similarity index (cosine)
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Metadata filter indexes
CREATE INDEX IF NOT EXISTS chunks_repo_idx      ON chunks (repo_owner, repo_name);
CREATE INDEX IF NOT EXISTS chunks_file_idx      ON chunks (file_path);
CREATE INDEX IF NOT EXISTS chunks_language_idx  ON chunks (language);
CREATE INDEX IF NOT EXISTS chunks_deleted_idx   ON chunks (is_deleted);

-- Full-text search (BM25-style keyword search on raw source)
CREATE INDEX IF NOT EXISTS chunks_fts_idx
    ON chunks USING GIN (to_tsvector('english', raw_content));

-- Fuzzy symbol name search
CREATE INDEX IF NOT EXISTS chunks_symbol_trgm_idx
    ON chunks USING GIN (symbol_name gin_trgm_ops);

-- ============================================================
-- TABLE: symbols — Exact & fuzzy symbol lookup
-- ============================================================
CREATE TABLE IF NOT EXISTS symbols (
    id               TEXT PRIMARY KEY,          -- "{file_path}:{qualified_name}"
    name             TEXT NOT NULL,             -- "authenticate"
    qualified_name   TEXT NOT NULL,             -- "UserService.authenticate"
    kind             TEXT NOT NULL,             -- function, class, method, variable
    file_path        TEXT NOT NULL,
    repo_owner       TEXT NOT NULL,
    repo_name        TEXT NOT NULL,
    start_line       INTEGER NOT NULL,
    end_line         INTEGER NOT NULL,
    signature        TEXT,                      -- "def authenticate(self, token: str) -> bool"
    docstring        TEXT,
    is_exported      BOOLEAN DEFAULT FALSE,
    indexed_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS symbols_name_idx          ON symbols (name);
CREATE INDEX IF NOT EXISTS symbols_qualified_idx     ON symbols (qualified_name);
CREATE INDEX IF NOT EXISTS symbols_repo_idx          ON symbols (repo_owner, repo_name);
CREATE INDEX IF NOT EXISTS symbols_name_trgm_idx     ON symbols USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS symbols_fts_idx           ON symbols USING GIN (
    to_tsvector('english', name || ' ' || COALESCE(qualified_name, ''))
);

-- ============================================================
-- TABLE: merkle_nodes — Change detection via GitHub blob SHA
-- ============================================================
-- GitHub's blob SHA IS the Merkle hash — we don't compute it ourselves.
-- Compare stored blob_sha vs payload SHA to skip re-indexing unchanged files.
CREATE TABLE IF NOT EXISTS merkle_nodes (
    file_path        TEXT NOT NULL,
    repo_owner       TEXT NOT NULL,
    repo_name        TEXT NOT NULL,
    blob_sha         TEXT NOT NULL,             -- GitHub blob SHA
    last_indexed     TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (file_path, repo_owner, repo_name)
);

-- ============================================================
-- TABLE: repos — Registered repositories
-- ============================================================
CREATE TABLE IF NOT EXISTS repos (
    id               SERIAL PRIMARY KEY,
    owner            TEXT NOT NULL,
    name             TEXT NOT NULL,
    branch           TEXT NOT NULL DEFAULT 'main',
    description      TEXT,
    registered_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_indexed     TIMESTAMP WITH TIME ZONE,
    status           TEXT DEFAULT 'pending',    -- pending, indexing, ready, error
    UNIQUE (owner, name)
);

-- ============================================================
-- TABLE: webhook_events — Audit log of incoming webhooks
-- ============================================================
CREATE TABLE IF NOT EXISTS webhook_events (
    id               SERIAL PRIMARY KEY,
    delivery_id      TEXT UNIQUE,               -- X-GitHub-Delivery header
    event_type       TEXT NOT NULL,             -- push, pull_request, etc.
    repo_owner       TEXT,
    repo_name        TEXT,
    commit_sha       TEXT,
    files_changed    INTEGER DEFAULT 0,
    status           TEXT DEFAULT 'queued',     -- queued, processing, done, error
    error_message    TEXT,
    received_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    processed_at     TIMESTAMP WITH TIME ZONE
);
