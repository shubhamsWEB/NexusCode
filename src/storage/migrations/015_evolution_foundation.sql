-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 015: Self-Evolution Foundation
-- Creates the four tables required for NexusCode's metacognition system:
--   1. interaction_metrics   — per-interaction telemetry (Mirror)
--   2. repo_worldviews       — versioned semantic understanding (Memory)
--   3. evolution_log         — reflection cycle audit trail (Engine)
--   4. ab_experiments        — A/B parameter experiment tracking (Engine)
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 1. Interaction Metrics ───────────────────────────────────────────────────
-- One row per Ask or Plan completion. Captures quality, latency, and the exact
-- retrieval parameter snapshot at the time of the call.

CREATE TABLE IF NOT EXISTS interaction_metrics (
    id                          SERIAL PRIMARY KEY,
    repo_owner                  TEXT NOT NULL,
    repo_name                   TEXT NOT NULL,
    interaction_type            TEXT NOT NULL CHECK (interaction_type IN ('ask', 'plan')),
    query                       TEXT NOT NULL,

    -- Quality signals
    implicit_quality_score      FLOAT,      -- 0.0–1.0 from cross-encoder reranker
    user_rating                 INTEGER,    -- explicit 1–5 star feedback (nullable)
    user_feedback_text          TEXT,       -- optional free-text from user

    -- Efficiency signals
    retrieval_iterations        INTEGER,
    tool_calls_count            INTEGER,
    context_tokens              INTEGER,
    answer_tokens               INTEGER,
    elapsed_ms                  FLOAT,
    query_complexity            TEXT,       -- 'simple' | 'moderate' | 'complex'

    -- Retrieval parameter snapshot (what was in use when this call ran)
    retrieval_strategy          TEXT,       -- 'semantic' | 'keyword' | 'hybrid'
    hnsw_ef_search_used         INTEGER,
    rrf_k_used                  INTEGER,
    candidate_multiplier_used   INTEGER,
    reranker_top_n_used         INTEGER,
    relevance_threshold_used    FLOAT,
    max_iterations_used         INTEGER,

    -- Linkage
    session_id                  UUID,       -- optional link to chat_sessions
    plan_id                     UUID,       -- optional link to plan_history

    created_at                  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_interaction_metrics_repo_time
    ON interaction_metrics (repo_owner, repo_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_interaction_metrics_type_time
    ON interaction_metrics (interaction_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_interaction_metrics_quality
    ON interaction_metrics (implicit_quality_score DESC)
    WHERE implicit_quality_score IS NOT NULL;


-- ── 2. Repo Worldviews ───────────────────────────────────────────────────────
-- Versioned LLM-generated semantic understanding of each repository.
-- Each reflection cycle or re-index can produce a new version.

CREATE TABLE IF NOT EXISTS repo_worldviews (
    id                      SERIAL PRIMARY KEY,
    repo_owner              TEXT NOT NULL,
    repo_name               TEXT NOT NULL,
    version                 INTEGER NOT NULL DEFAULT 1,

    -- Semantic content
    architecture_summary    TEXT,           -- 2–3 paragraph overview
    key_patterns            TEXT[],         -- ["factory pattern", "event-driven", ...]
    difficult_zones         TEXT[],         -- ["async error handling", ...]
    conventions             TEXT[],         -- ["snake_case", "type hints everywhere", ...]
    recent_changes          TEXT,           -- summary of latest indexed changes
    full_worldview          TEXT NOT NULL,  -- complete narrative worldview document

    -- Generation metadata
    chunks_sampled          INTEGER,        -- how many chunks were used to generate this
    interactions_analyzed   INTEGER,        -- how many interactions informed this
    model_used              TEXT,           -- which LLM generated this
    generated_at            TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE (repo_owner, repo_name, version)
);

CREATE INDEX IF NOT EXISTS idx_repo_worldviews_latest
    ON repo_worldviews (repo_owner, repo_name, version DESC);


-- ── 3. Evolution Log ─────────────────────────────────────────────────────────
-- Audit trail for each self-reflection cycle. One row per cycle per repo.

CREATE TABLE IF NOT EXISTS evolution_log (
    id                          SERIAL PRIMARY KEY,
    repo_owner                  TEXT NOT NULL,
    repo_name                   TEXT NOT NULL,
    cycle_number                INTEGER NOT NULL,

    -- Timing
    cycle_started_at            TIMESTAMPTZ DEFAULT NOW(),
    cycle_completed_at          TIMESTAMPTZ,

    -- Analysis scope
    metrics_analyzed_count      INTEGER,        -- interactions examined
    lookback_days               INTEGER,        -- window used

    -- Outcome counts
    improvements_proposed       INTEGER DEFAULT 0,
    improvements_applied        INTEGER DEFAULT 0,

    -- Change details (JSON arrays / objects)
    parameter_changes           JSONB,  -- [{"param": "hnsw_ef_search", "old": 40, "new": 60, "reason": "..."}]
    prompt_changes              JSONB,  -- [{"target": "ask_system", "old": "...", "new": "...", "reason": "..."}]
    discovered_patterns         TEXT[], -- new insights this cycle

    -- Worldview
    new_worldview_version       INTEGER,        -- version created this cycle (nullable)

    -- Status
    status                      TEXT NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending','analyzing','complete','failed')),
    error_message               TEXT,

    UNIQUE (repo_owner, repo_name, cycle_number)
);

CREATE INDEX IF NOT EXISTS idx_evolution_log_repo_time
    ON evolution_log (repo_owner, repo_name, cycle_completed_at DESC);


-- ── 4. A/B Experiments ───────────────────────────────────────────────────────
-- Tracks controlled experiments on retrieval parameters.

CREATE TABLE IF NOT EXISTS ab_experiments (
    id                      SERIAL PRIMARY KEY,
    repo_owner              TEXT NOT NULL,
    repo_name               TEXT NOT NULL,
    experiment_name         TEXT NOT NULL,
    parameter_name          TEXT NOT NULL,

    -- Values under test
    control_value           TEXT NOT NULL,      -- baseline (serialised)
    treatment_value         TEXT NOT NULL,      -- candidate (serialised)

    -- Timing
    started_at              TIMESTAMPTZ DEFAULT NOW(),
    ended_at                TIMESTAMPTZ,

    -- Samples
    control_sample_count    INTEGER DEFAULT 0,
    treatment_sample_count  INTEGER DEFAULT 0,

    -- Aggregated outcomes
    control_avg_quality     FLOAT,
    treatment_avg_quality   FLOAT,
    control_avg_latency_ms  FLOAT,
    treatment_avg_latency_ms FLOAT,

    -- Verdict
    winner                  TEXT CHECK (winner IN ('control','treatment','inconclusive')),
    confidence              FLOAT,          -- 0.0–1.0 (higher = more data)
    status                  TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active','completed','rolled_back')),

    -- Linked to originating cycle
    evolution_cycle_id      INTEGER REFERENCES evolution_log(id) ON DELETE SET NULL,

    UNIQUE (repo_owner, repo_name, experiment_name)
);

CREATE INDEX IF NOT EXISTS idx_ab_experiments_active
    ON ab_experiments (repo_owner, repo_name, status)
    WHERE status = 'active';
