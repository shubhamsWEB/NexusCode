-- src/storage/migrations/003_chat_history.sql
-- SAFE: only creates new tables, never modifies existing ones

CREATE TABLE IF NOT EXISTS chat_sessions (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    repo_owner     TEXT,
    repo_name      TEXT,
    turn_count     INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_active_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_created ON chat_sessions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_repo    ON chat_sessions (repo_owner, repo_name);

CREATE TABLE IF NOT EXISTS chat_turns (
    id              SERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    turn_index      INTEGER NOT NULL,
    user_query      TEXT NOT NULL,
    answer          TEXT NOT NULL,
    cited_files     TEXT[],
    follow_up_hints TEXT[],
    elapsed_ms      FLOAT,
    context_tokens  INTEGER,
    context_files   INTEGER,
    query_complexity TEXT,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (session_id, turn_index)
);
CREATE INDEX IF NOT EXISTS idx_chat_turns_session ON chat_turns (session_id, turn_index);

CREATE TABLE IF NOT EXISTS plan_history (
    id                SERIAL PRIMARY KEY,
    plan_id           TEXT NOT NULL UNIQUE,
    query             TEXT NOT NULL,
    response_type     TEXT NOT NULL,
    repo_owner        TEXT,
    repo_name         TEXT,
    plan_json         TEXT NOT NULL,
    elapsed_ms        FLOAT,
    context_tokens    INTEGER,
    web_research_used BOOLEAN DEFAULT FALSE,
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_plan_history_created ON plan_history (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_plan_history_repo    ON plan_history (repo_owner, repo_name);
