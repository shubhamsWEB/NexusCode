ALTER TABLE chunks ADD COLUMN IF NOT EXISTS parent_chunk_id TEXT;
CREATE INDEX IF NOT EXISTS chunks_parent_idx
    ON chunks (parent_chunk_id) WHERE parent_chunk_id IS NOT NULL;
