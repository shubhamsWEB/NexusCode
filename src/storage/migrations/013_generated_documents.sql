-- Migration 013: generated_documents table for PDF storage
-- Run: psql $DATABASE_URL -f src/storage/migrations/013_generated_documents.sql

CREATE TABLE IF NOT EXISTS generated_documents (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    run_id      TEXT REFERENCES workflow_runs(id) ON DELETE CASCADE,
    step_id     TEXT,
    title       TEXT NOT NULL,
    filename    TEXT NOT NULL,
    pdf_bytes   BYTEA NOT NULL,
    size_bytes  INTEGER NOT NULL,
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_generated_documents_run_id
    ON generated_documents(run_id);

CREATE INDEX IF NOT EXISTS idx_generated_documents_step
    ON generated_documents(run_id, step_id);
