-- Aether initial schema.
-- Plaintext columns are non-sensitive metadata (timestamps, hashes, scores,
-- statuses); every human-readable content column is stored encrypted (*_enc).

CREATE TABLE IF NOT EXISTS events (
    id             BIGSERIAL PRIMARY KEY,
    source         TEXT        NOT NULL,
    kind           TEXT        NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash   TEXT        NOT NULL,
    salience_score REAL        NOT NULL DEFAULT 0,
    memorable      BOOLEAN     NOT NULL DEFAULT FALSE,
    payload_enc    BYTEA       NOT NULL,
    meta           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source, content_hash)
);
CREATE INDEX IF NOT EXISTS events_memorable_recent_idx
    ON events (occurred_at DESC) WHERE memorable;

CREATE TABLE IF NOT EXISTS identities (
    id             BIGSERIAL PRIMARY KEY,
    display_name   TEXT        NOT NULL,
    confidence     REAL        NOT NULL DEFAULT 1.0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    merged_into_id BIGINT REFERENCES identities(id)
);

CREATE TABLE IF NOT EXISTS identity_handles (
    id          BIGSERIAL PRIMARY KEY,
    identity_id BIGINT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
    platform    TEXT   NOT NULL,
    raw_handle  TEXT   NOT NULL,
    normalized   TEXT   NOT NULL,
    UNIQUE (platform, normalized)
);
CREATE INDEX IF NOT EXISTS identity_handles_identity_idx ON identity_handles (identity_id);

CREATE TABLE IF NOT EXISTS entity_notes (
    id          BIGSERIAL PRIMARY KEY,
    identity_id BIGINT NOT NULL REFERENCES identities(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload_enc BYTEA  NOT NULL
);
CREATE INDEX IF NOT EXISTS entity_notes_identity_idx ON entity_notes (identity_id, created_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id          BIGSERIAL PRIMARY KEY,
    surface     TEXT   NOT NULL,
    direction   TEXT   NOT NULL,           -- 'in' | 'out'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload_enc BYTEA  NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_approvals (
    id          BIGSERIAL PRIMARY KEY,
    tool_name   TEXT   NOT NULL,
    params_enc  BYTEA  NOT NULL,
    status      TEXT   NOT NULL DEFAULT 'pending',   -- pending|approved|denied|expired|executed
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at  TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ NOT NULL,
    decided_by  TEXT
);

CREATE TABLE IF NOT EXISTS scheduled_actions (
    id            BIGSERIAL PRIMARY KEY,
    label         TEXT   NOT NULL,
    run_at        TIMESTAMPTZ NOT NULL,
    status        TEXT   NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    payload_enc   BYTEA  NOT NULL,
    result_digest TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS scheduled_actions_due_idx
    ON scheduled_actions (run_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS audit_log (
    seq           BIGSERIAL PRIMARY KEY,
    actor         TEXT        NOT NULL,    -- agent|user|scheduler|system
    tool_name     TEXT        NOT NULL,
    decision      TEXT        NOT NULL,    -- allow|approve|deny|filtered|info
    rules_matched TEXT        NOT NULL DEFAULT '',
    params_digest TEXT        NOT NULL DEFAULT '',
    outcome       TEXT        NOT NULL DEFAULT '',
    prev_hash     TEXT        NOT NULL,
    entry_hash    TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS connector_cursors (
    source    TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL
);
