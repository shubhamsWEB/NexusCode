-- External MCP Server registry. Safe to re-run (IF NOT EXISTS throughout).
CREATE TABLE IF NOT EXISTS external_mcp_servers (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    url          TEXT NOT NULL UNIQUE,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    auth_header  TEXT,        -- e.g. "Bearer sk-abc..." (stored as-is)
    description  TEXT,        -- user-provided notes
    tool_count   INTEGER DEFAULT 0,
    last_seen_at TIMESTAMPTZ,
    last_error   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ext_mcp_enabled ON external_mcp_servers (enabled);
