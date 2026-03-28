-- Migration 020: Integration credentials table for enterprise tool OAuth
--
-- Stores encrypted credentials for external services (Jira, Slack, GitHub, Figma, Notion).
-- Tokens are AES-256-GCM encrypted by the application layer before storage.
-- The DB never holds plaintext tokens.

CREATE TABLE IF NOT EXISTS integration_credentials (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              TEXT        NOT NULL DEFAULT 'default',
    service             TEXT        NOT NULL,       -- 'jira' | 'slack' | 'github' | 'figma' | 'notion'
    auth_type           TEXT        NOT NULL,       -- 'service_account' | 'oauth_user'
    user_id             TEXT,                       -- NULL for service accounts

    -- Encrypted tokens (AES-256-GCM: nonce || ciphertext || tag)
    access_token_enc    BYTEA       NOT NULL,
    refresh_token_enc   BYTEA,
    token_expires_at    TIMESTAMPTZ,

    -- Service-specific metadata (cloud_id for Jira, workspace_id for Slack, etc.)
    metadata            JSONB       NOT NULL DEFAULT '{}',
    scopes              TEXT[]      NOT NULL DEFAULT '{}',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One service_account per service per org; one oauth_user per user per service per org
-- PostgreSQL requires a UNIQUE INDEX (not constraint) for expression-based uniqueness
CREATE UNIQUE INDEX IF NOT EXISTS uq_integration_cred
    ON integration_credentials (org_id, service, COALESCE(user_id, 'service'));

CREATE INDEX IF NOT EXISTS idx_integration_creds_org_service
    ON integration_credentials (org_id, service);

COMMENT ON TABLE integration_credentials IS
    'AES-256-GCM encrypted OAuth and API tokens for enterprise integrations. Tokens never stored in plaintext.';

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_integration_credentials_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_integration_credentials_updated_at ON integration_credentials;
CREATE TRIGGER trg_integration_credentials_updated_at
    BEFORE UPDATE ON integration_credentials
    FOR EACH ROW EXECUTE FUNCTION update_integration_credentials_updated_at();
