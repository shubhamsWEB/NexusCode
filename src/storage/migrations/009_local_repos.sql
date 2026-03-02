-- 009_local_repos.sql
-- Add source_type and local_path columns to repos table.
-- Safe to re-run (IF NOT EXISTS / idempotent).
-- Existing rows get source_type = 'github' and local_path = NULL automatically.

ALTER TABLE repos
    ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'github',
    ADD COLUMN IF NOT EXISTS local_path  TEXT;
