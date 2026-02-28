-- Add an index for keyword searching on enriched_content
-- This replaces the requirement to search only raw_content, enabling matches on LLM summaries and headers

CREATE INDEX IF NOT EXISTS chunks_enriched_content_fts_idx 
    ON chunks USING GIN (to_tsvector('english', enriched_content));
