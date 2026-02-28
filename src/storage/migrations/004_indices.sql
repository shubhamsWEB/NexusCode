-- Performance indices missing from 001_init.sql
-- Safe to run multiple times (IF NOT EXISTS)

-- Partial index for active chunks — speeds up all retrieval queries
CREATE INDEX IF NOT EXISTS chunks_not_deleted_idx
    ON chunks (repo_owner, repo_name) WHERE NOT is_deleted;

-- Speeds up retriever Phase 4 file-structure lookups
CREATE INDEX IF NOT EXISTS symbols_file_path_idx
    ON symbols (file_path, repo_owner, repo_name);

-- Speeds up dashboard webhook event listing
CREATE INDEX IF NOT EXISTS webhook_events_status_idx
    ON webhook_events (status, received_at DESC);
