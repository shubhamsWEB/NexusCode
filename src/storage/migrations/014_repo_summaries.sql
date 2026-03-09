-- Migration 014: Repo summaries for routing + API key scopes for team-level repo gating

-- ─── Repo Summaries (for query-time routing) ────────────────────────────────
CREATE TABLE IF NOT EXISTS repo_summaries (
    repo_owner           TEXT NOT NULL,
    repo_name            TEXT NOT NULL,
    centroid_embedding   vector(1536),          -- AVG(embedding) of all active chunks
    tech_stack_keywords  TEXT[]  DEFAULT '{}',  -- top-50 frequent tokens from enriched_content
    language_distribution JSONB  DEFAULT '{}',  -- {"python": 0.72, "typescript": 0.28}
    chunk_count          INTEGER DEFAULT 0,
    updated_at           TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (repo_owner, repo_name)
);

CREATE INDEX IF NOT EXISTS repo_summaries_centroid_idx
    ON repo_summaries USING hnsw (centroid_embedding vector_cosine_ops)
    WITH (m = 8, ef_construction = 32);

-- ─── API Key Scopes (team-level repo access control) ────────────────────────
CREATE TABLE IF NOT EXISTS api_key_scopes (
    id           SERIAL PRIMARY KEY,
    key_hash     TEXT NOT NULL UNIQUE,          -- SHA-256(raw_key), raw key never stored
    name         TEXT NOT NULL,                 -- e.g. "frontend-team"
    description  TEXT,
    -- Allowed repos as flat "owner/name" strings. NULL or empty means all repos (admin).
    allowed_repos TEXT[] DEFAULT '{}',
    created_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_used_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS api_key_scopes_hash_idx ON api_key_scopes (key_hash);
