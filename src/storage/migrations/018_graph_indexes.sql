-- Migration 018: Performance indexes for knowledge-graph BFS queries
--
-- The new graph_expander and find_callers features query kg_edges with
-- patterns using ANY(array) for edge_type + source/target lookups.
-- The existing indexes on (repo_owner, repo_name) and individual
-- source_id / target_id columns are not optimal for these composite
-- lookups. These covering indexes eliminate sequential scans on large
-- kg_edges tables (100K+ edges in typical production repos).

-- Index 1: BFS backward-lookup — "who calls symbol X?"
-- Covers edge_type = 'calls' AND target_id = ANY(array) AND repo_owner/name
CREATE INDEX IF NOT EXISTS idx_kg_edges_calls_target
    ON kg_edges (repo_owner, repo_name, edge_type, target_id)
    WHERE edge_type = 'calls';

-- Index 2: BFS forward-lookup — "what does symbol X call?"
-- Covers edge_type = 'calls' AND source_id = ANY(array) AND repo_owner/name
CREATE INDEX IF NOT EXISTS idx_kg_edges_calls_source
    ON kg_edges (repo_owner, repo_name, edge_type, source_id)
    WHERE edge_type = 'calls';

-- Index 3: Imports forward-lookup — "what files does file X import?"
-- Covers edge_type = 'imports' AND source_id = ANY(array) AND repo_owner/name
CREATE INDEX IF NOT EXISTS idx_kg_edges_imports_source
    ON kg_edges (repo_owner, repo_name, edge_type, source_id)
    WHERE edge_type = 'imports';

-- Index 4: Semantic edge lookups — used by get_semantic_context
CREATE INDEX IF NOT EXISTS idx_kg_edges_semantic_source
    ON kg_edges (repo_owner, repo_name, source_id)
    WHERE edge_type = 'semantic';

CREATE INDEX IF NOT EXISTS idx_kg_edges_semantic_target
    ON kg_edges (repo_owner, repo_name, target_id)
    WHERE edge_type = 'semantic';

-- Index 5: Stale-edge deletion covers the sub-SELECT on symbols.file_path
CREATE INDEX IF NOT EXISTS idx_symbols_repo_file
    ON symbols (repo_owner, repo_name, file_path);
