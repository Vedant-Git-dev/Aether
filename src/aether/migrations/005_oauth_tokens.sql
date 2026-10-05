-- OAuth tokens: what a provider handed back at the /oauth/callback, one row
-- per provider. The user never types these in chat — they arrive
-- provider→callback during an /apps sign-in — so the database (not .env) is
-- their home, encrypted with the same AES-256-GCM key as all other content
-- and bound to their row through the cipher's AAD. The reserved names
-- GOOGLE_OAUTH_ACCESS_TOKEN resolve through this table via secret_env, so
-- an MCP server's Bearer header references the token by name only, exactly
-- like a key that lives in .env.

CREATE TABLE IF NOT EXISTS oauth_tokens (
    provider    TEXT PRIMARY KEY,
    access_enc  BYTEA NOT NULL,
    refresh_enc BYTEA NOT NULL,
    scopes      TEXT NOT NULL DEFAULT '',
    expires_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
