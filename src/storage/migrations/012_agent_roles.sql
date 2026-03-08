-- Agent Role Overrides: custom configs for built-in roles and fully custom roles.
-- Built-in roles fall back to src/agent/roles.py hardcoded defaults when no row exists.

CREATE TABLE IF NOT EXISTS agent_role_overrides (
    name            TEXT        PRIMARY KEY,
    display_name    TEXT        NOT NULL DEFAULT '',
    description     TEXT        NOT NULL DEFAULT '',
    system_prompt   TEXT        NOT NULL DEFAULT '',
    instructions    TEXT        NOT NULL DEFAULT '',
    default_tools   TEXT[]      NOT NULL DEFAULT '{}',
    require_search  BOOLEAN     NOT NULL DEFAULT TRUE,
    max_iterations  INTEGER     NOT NULL DEFAULT 5,
    token_budget    INTEGER     NOT NULL DEFAULT 80000,
    is_builtin      BOOLEAN     NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
