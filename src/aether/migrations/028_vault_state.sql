-- Vault installation state, signed manifest history and one-use credentials.
--
-- Only public protocol metadata, opaque hashes and ciphertext envelopes live
-- here. No category key, grant box, recovery phrase or user-derived value is
-- stored in a plain column.

CREATE TABLE IF NOT EXISTS vault_state (
    vault_id   BYTEA PRIMARY KEY,
    device_id  BYTEA NOT NULL,
    epoch      BIGINT NOT NULL CHECK (epoch >= 1),
    revision   BIGINT NOT NULL CHECK (revision >= 1),
    digest     BYTEA NOT NULL,
    manifest   BYTEA NOT NULL,
    pins       BYTEA NOT NULL,
    state      TEXT NOT NULL CHECK (state IN ('unpaired', 'locked', 'available', 'revoked')),
    generation BIGINT NOT NULL DEFAULT 1 CHECK (generation >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS vault_state_single_installation
    ON vault_state ((1));

CREATE TABLE IF NOT EXISTS vault_manifest (
    vault_id        BYTEA NOT NULL REFERENCES vault_state(vault_id) ON DELETE CASCADE,
    revision        BIGINT NOT NULL CHECK (revision >= 1),
    digest          BYTEA NOT NULL,
    previous_digest BYTEA,
    manifest        BYTEA NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vault_id, revision),
    UNIQUE (vault_id, digest)
);

CREATE TABLE IF NOT EXISTS vault_aead_budget (
    vault_id BYTEA NOT NULL REFERENCES vault_state(vault_id) ON DELETE CASCADE,
    category TEXT NOT NULL CHECK (
        category IN ('credentials', 'media', 'memory', 'operations', 'workspace')
    ),
    version  BIGINT NOT NULL CHECK (version >= 1),
    reserved BIGINT NOT NULL CHECK (reserved > 0 AND reserved < 4294967296),
    PRIMARY KEY (vault_id, category, version)
);

CREATE TABLE IF NOT EXISTS vault_ticket (
    ticket_hash BYTEA PRIMARY KEY,
    vault_id    BYTEA NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS vault_ticket_open_idx
    ON vault_ticket (expires_at) WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS vault_challenge (
    challenge_hash BYTEA PRIMARY KEY,
    vault_id       BYTEA NOT NULL,
    device_id      BYTEA NOT NULL,
    purpose        TEXT NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    consumed_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS vault_challenge_open_idx
    ON vault_challenge (expires_at) WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS vault_session (
    session_hash BYTEA PRIMARY KEY,
    vault_id     BYTEA NOT NULL,
    device_id    BYTEA,
    kind         TEXT NOT NULL CHECK (kind IN ('device', 'panel')),
    epoch        BIGINT NOT NULL CHECK (epoch >= 1),
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS vault_session_open_idx
    ON vault_session (expires_at) WHERE revoked_at IS NULL;
