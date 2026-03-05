-- Migration 009: Knowledge Graph edges table
-- Run: psql $DATABASE_URL -f src/storage/migrations/009_knowledge_graph.sql

CREATE TABLE IF NOT EXISTS kg_edges (
    id          BIGSERIAL PRIMARY KEY,
    source_id   TEXT NOT NULL,   -- file_path or symbol qualified_name
    source_type TEXT NOT NULL,   -- 'file' | 'symbol'
    target_id   TEXT NOT NULL,
    target_type TEXT NOT NULL,
    edge_type   TEXT NOT NULL,   -- 'imports' | 'defines' | 'contains' | 'calls'
    repo_owner  TEXT NOT NULL,
    repo_name   TEXT NOT NULL,
    confidence  FLOAT   DEFAULT 1.0,
    extra       JSONB,
    indexed_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_id, target_id, edge_type, repo_owner, repo_name)
);

CREATE INDEX IF NOT EXISTS kg_edges_repo_idx   ON kg_edges(repo_owner, repo_name);
CREATE INDEX IF NOT EXISTS kg_edges_source_idx ON kg_edges(source_id, repo_owner, repo_name);
CREATE INDEX IF NOT EXISTS kg_edges_type_idx   ON kg_edges(edge_type, repo_owner, repo_name);
