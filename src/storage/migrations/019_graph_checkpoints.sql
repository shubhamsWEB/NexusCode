-- Migration 019: LangGraph checkpoint tables + graph_state column on workflow_runs
--
-- LangGraph PostgreSQL checkpointer requires three tables to persist workflow
-- state across node executions, enabling resume-after-failure and human
-- checkpoint pause/resume for long-running enterprise workflows.
--
-- These tables are also created automatically by AsyncPostgresSaver.setup(),
-- but including them here ensures the schema is managed by our migration system
-- and is available even before the first app boot.
--
-- Reference: langgraph-checkpoint-postgres v2.x schema

-- ── LangGraph core checkpoint table ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id           TEXT        NOT NULL,
    checkpoint_ns       TEXT        NOT NULL DEFAULT '',
    checkpoint_id       TEXT        NOT NULL,
    parent_checkpoint_id TEXT,
    type                TEXT,
    checkpoint          JSONB       NOT NULL,
    metadata            JSONB       NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_thread
    ON checkpoints (thread_id, checkpoint_ns);

-- ── Channel value blobs (LangGraph state channels) ───────────────────────────
CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id     TEXT  NOT NULL,
    checkpoint_ns TEXT  NOT NULL DEFAULT '',
    channel       TEXT  NOT NULL,
    version       TEXT  NOT NULL,
    type          TEXT  NOT NULL,
    blob          BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

-- ── Pending writes (in-flight node outputs before checkpoint commit) ──────────
CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id     TEXT    NOT NULL,
    checkpoint_ns TEXT    NOT NULL DEFAULT '',
    checkpoint_id TEXT    NOT NULL,
    task_id       TEXT    NOT NULL,
    idx           INTEGER NOT NULL,
    channel       TEXT    NOT NULL,
    type          TEXT,
    blob          BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

-- ── Migrations table (LangGraph internal) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS checkpoint_migrations (
    v INTEGER PRIMARY KEY
);

-- ── Extend workflow_runs with graph_state for full state persistence ──────────
ALTER TABLE workflow_runs
    ADD COLUMN IF NOT EXISTS graph_state JSONB DEFAULT NULL;

COMMENT ON COLUMN workflow_runs.graph_state IS
    'Final GraphState snapshot after workflow completion (enterprise graph-style runs only)';
