-- Replace ivfflat with HNSW for 75x-150x faster ANN search.
-- pgvector 0.5.0+ required. Run during a maintenance window.
-- Safe to run multiple times (IF NOT EXISTS / IF EXISTS guards).

-- Step 1: Drop old ivfflat index
DROP INDEX IF EXISTS chunks_embedding_idx;

-- Step 2: Create HNSW index (m=16, ef_construction=64 are pgvector defaults)
-- m: max connections per node (higher = better recall, more memory)
-- ef_construction: search width during build (higher = better quality index, slower build)
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
