-- Direct provider connections. One row per provider (one user, one Aether
-- instance). Tokens are encrypted with the dedicated provider-token keyring;
-- key_id names the key version and is part of the AAD, so a ciphertext can
-- never be moved between providers, columns, or key versions.

CREATE TABLE IF NOT EXISTS provider_connections (
    provider             TEXT PRIMARY KEY,
    auth_mode            TEXT NOT NULL DEFAULT 'oauth'
                             CHECK (auth_mode IN ('oauth', 'token')),
    account_label        TEXT NOT NULL DEFAULT '',
    account_id           TEXT NOT NULL DEFAULT '',
    access_enc           BYTEA NOT NULL,
    refresh_enc          BYTEA,
    key_id               TEXT NOT NULL,
    token_type           TEXT NOT NULL DEFAULT 'Bearer',
    scopes               TEXT NOT NULL DEFAULT '',
    expires_at           TIMESTAMPTZ,
    refresh_expires_at   TIMESTAMPTZ,
    status               TEXT NOT NULL DEFAULT 'active',
    last_refreshed_at    TIMESTAMPTZ,
    last_error           TEXT NOT NULL DEFAULT '',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS provider_oauth_states (
    state_hash        TEXT PRIMARY KEY,
    provider          TEXT NOT NULL,
    app               TEXT NOT NULL,
    scopes            TEXT NOT NULL,
    code_verifier_enc BYTEA NOT NULL,
    key_id            TEXT NOT NULL,
    redirect_uri      TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL,
    consumed_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS provider_sync_state (
    provider   TEXT NOT NULL,
    app        TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, app, key)
);
