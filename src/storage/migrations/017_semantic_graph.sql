-- Migration 017: Semantic graph partial indexes
-- Adds fast lookup indexes for semantic edges on kg_edges.
-- No schema changes needed — edge_type='semantic' + existing extra JSONB column is sufficient.

CREATE INDEX IF NOT EXISTS kg_edges_semantic_idx
  ON kg_edges (repo_owner, repo_name, source_id)
  WHERE edge_type = 'semantic';

CREATE INDEX IF NOT EXISTS kg_edges_semantic_target_idx
  ON kg_edges (repo_owner, repo_name, target_id)
  WHERE edge_type = 'semantic';
