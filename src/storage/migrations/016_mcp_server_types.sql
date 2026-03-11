-- Migration 016: MCP server types + auth_type + OAuth + stdio support
-- Run: psql $DATABASE_URL < src/storage/migrations/016_mcp_server_types.sql

ALTER TABLE external_mcp_servers
  ADD COLUMN IF NOT EXISTS server_type   TEXT NOT NULL DEFAULT 'remote',   -- remote | stdio
  ADD COLUMN IF NOT EXISTS command       TEXT,          -- stdio: executable (e.g. npx)
  ADD COLUMN IF NOT EXISTS args          JSONB NOT NULL DEFAULT '[]',       -- stdio: arg list
  ADD COLUMN IF NOT EXISTS env           JSONB NOT NULL DEFAULT '{}',       -- stdio: env vars
  ADD COLUMN IF NOT EXISTS auth_type     TEXT NOT NULL DEFAULT 'header',    -- none|header|bearer|basic|oauth
  ADD COLUMN IF NOT EXISTS oauth_client_id     TEXT,
  ADD COLUMN IF NOT EXISTS oauth_token         TEXT,
  ADD COLUMN IF NOT EXISTS oauth_refresh_token TEXT,
  ADD COLUMN IF NOT EXISTS oauth_expires_at    TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS oauth_token_endpoint TEXT,
  ADD COLUMN IF NOT EXISTS oauth_code_verifier  TEXT;   -- temp during OAuth dance

-- Make url optional (stdio servers have no URL)
ALTER TABLE external_mcp_servers ALTER COLUMN url DROP NOT NULL;

-- Drop old unique constraint on url if it exists (handle both constraint names)
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'external_mcp_servers_url_key'
      AND conrelid = 'external_mcp_servers'::regclass
  ) THEN
    ALTER TABLE external_mcp_servers DROP CONSTRAINT external_mcp_servers_url_key;
  END IF;
END $$;

-- Partial unique index: enforce uniqueness only when url is set
CREATE UNIQUE INDEX IF NOT EXISTS ix_ext_mcp_url_unique
  ON external_mcp_servers (url) WHERE url IS NOT NULL;
