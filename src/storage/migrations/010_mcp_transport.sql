-- Add transport column to external_mcp_servers.
-- Safe to re-run (uses IF NOT EXISTS / DO NOTHING patterns).
--
-- transport values:
--   'auto'            — try Streamable HTTP first, fall back to SSE (default)
--   'streamable_http' — new MCP transport (Context7, most cloud-hosted MCP servers)
--   'sse'             — legacy SSE transport (self-hosted, older servers)

ALTER TABLE external_mcp_servers
    ADD COLUMN IF NOT EXISTS transport TEXT NOT NULL DEFAULT 'auto';
