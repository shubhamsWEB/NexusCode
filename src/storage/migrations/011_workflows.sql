-- 011_workflows.sql
-- Codebase Automation Engine — workflow state tables
--
-- Uses TEXT columns for status/type fields to avoid DO-block enum creation,
-- which is incompatible with the semicolon-split migration runner in
-- scripts/init_db.py (line 38) splits on semicolons, so no dollar-quoted DO blocks.
-- All enum validation is enforced at the Pydantic layer (src/workflows/models.py).

-- ── Workflow Definitions ──────────────────────────────────────────────────────
-- trigger_type: 'webhook' | 'schedule' | 'manual' | 'event'

CREATE TABLE IF NOT EXISTS workflow_definitions (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL DEFAULT '',
    yaml_definition TEXT NOT NULL,
    trigger_type    TEXT NOT NULL DEFAULT 'manual',
    trigger_config  JSONB NOT NULL DEFAULT '{}',
    version         INTEGER NOT NULL DEFAULT 1,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Workflow Runs ─────────────────────────────────────────────────────────────
-- status: 'pending' | 'running' | 'waiting_human' | 'completed' | 'failed' | 'cancelled'

CREATE TABLE IF NOT EXISTS workflow_runs (
    id                TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    workflow_id       TEXT NOT NULL REFERENCES workflow_definitions(id) ON DELETE CASCADE,
    workflow_name     TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    trigger_payload   JSONB NOT NULL DEFAULT '{}',
    context           JSONB NOT NULL DEFAULT '{}',
    started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ,
    total_tokens_used INTEGER NOT NULL DEFAULT 0,
    error_message     TEXT,
    result            JSONB
);

-- ── Step Executions ───────────────────────────────────────────────────────────
-- status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped'
-- step_type: 'agent' | 'action' | 'human_checkpoint'
-- agent_role: 'searcher' | 'planner' | 'reviewer' | 'coder' | 'tester' | 'supervisor'

CREATE TABLE IF NOT EXISTS step_executions (
    id            TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    run_id        TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_id       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    agent_role    TEXT,
    step_type     TEXT NOT NULL DEFAULT 'agent',
    input_context JSONB NOT NULL DEFAULT '{}',
    output        JSONB,
    tokens_used   INTEGER NOT NULL DEFAULT 0,
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    UNIQUE (run_id, step_id)
);

-- ── Human Checkpoints ─────────────────────────────────────────────────────────
-- status: 'waiting' | 'answered' | 'timed_out'

CREATE TABLE IF NOT EXISTS human_checkpoints (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    run_id      TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_id     TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    options     JSONB NOT NULL DEFAULT '[]',
    response    TEXT,
    status      TEXT NOT NULL DEFAULT 'waiting',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    answered_at TIMESTAMPTZ,
    timeout_at  TIMESTAMPTZ
);

-- ── Indices ───────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow_id ON workflow_runs(workflow_id);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status      ON workflow_runs(status);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_started_at  ON workflow_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_step_executions_run_id    ON step_executions(run_id);
CREATE INDEX IF NOT EXISTS idx_step_executions_status    ON step_executions(status);
CREATE INDEX IF NOT EXISTS idx_human_checkpoints_run_id  ON human_checkpoints(run_id);
CREATE INDEX IF NOT EXISTS idx_human_checkpoints_status  ON human_checkpoints(status);
