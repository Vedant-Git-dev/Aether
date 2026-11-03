-- Sealed storage, expand phase.
--
-- Every user-derived value gets a sealed column, a key version, and — where
-- equality lookup is needed — a keyed blind-index token. The old plaintext
-- columns stay in place so this migration can land before the stores are
-- rewritten; `018_sealed_cutover.sql` drops them once the backfill reports
-- converged and the new readers are live.
--
-- Plaintext columns added here are exactly the allowlist in
-- `memory/allowlist.py`: ids, key versions, blind tokens, status vocabulary
-- and storage bookkeeping. Nothing a user wrote or received.

-- ---------------------------------------------------------------------------
-- events: payload stays sealed; meta (source, kind, timestamps, salience,
-- summary, urgency, ...) moves into meta_enc. Dedup becomes a keyed tag and
-- the source is a blind token, because the plan seals sources too.
-- ---------------------------------------------------------------------------
ALTER TABLE events ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE events ADD COLUMN IF NOT EXISTS source_token BYTEA;
ALTER TABLE events ADD COLUMN IF NOT EXISTS dedup_mac BYTEA;
ALTER TABLE events ADD COLUMN IF NOT EXISTS meta_enc BYTEA;
ALTER TABLE events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE UNIQUE INDEX IF NOT EXISTS events_token_dedup_idx
    ON events (source_token, dedup_mac)
    WHERE source_token IS NOT NULL AND dedup_mac IS NOT NULL;

-- ---------------------------------------------------------------------------
-- identities / handles: names and addresses sealed; equality lookups use
-- tokens derived under their own purpose subkeys.
-- ---------------------------------------------------------------------------
ALTER TABLE identities ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE identities ADD COLUMN IF NOT EXISTS profile_enc BYTEA;

ALTER TABLE identity_handles ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE identity_handles ADD COLUMN IF NOT EXISTS platform_token BYTEA;
ALTER TABLE identity_handles ADD COLUMN IF NOT EXISTS normalized_token BYTEA;
ALTER TABLE identity_handles ADD COLUMN IF NOT EXISTS handle_enc BYTEA;
CREATE UNIQUE INDEX IF NOT EXISTS identity_handles_token_idx
    ON identity_handles (platform_token, normalized_token)
    WHERE platform_token IS NOT NULL AND normalized_token IS NOT NULL;

ALTER TABLE entity_notes ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';

-- ---------------------------------------------------------------------------
-- chat transcripts: the surface is a source, so it becomes a token.
-- ---------------------------------------------------------------------------
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS surface_token BYTEA;

-- ---------------------------------------------------------------------------
-- approvals: why a call was parked is content too.
-- ---------------------------------------------------------------------------
ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS rules_matched_enc BYTEA;
ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS note_enc BYTEA;

-- ---------------------------------------------------------------------------
-- scheduled actions: the label and the run time move into one sealed blob;
-- the worker loads pending rows at boot and filters them in memory.
-- ---------------------------------------------------------------------------
ALTER TABLE scheduled_actions ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE scheduled_actions ADD COLUMN IF NOT EXISTS meta_enc BYTEA;

-- ---------------------------------------------------------------------------
-- routines, traces, chunks, proactive tasks, media, provider state.
-- ---------------------------------------------------------------------------
ALTER TABLE routines ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE routines ADD COLUMN IF NOT EXISTS label_enc BYTEA;

ALTER TABLE decision_traces ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE decision_traces ADD COLUMN IF NOT EXISTS label_enc BYTEA;

ALTER TABLE memory_chunks ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE memory_chunks ADD COLUMN IF NOT EXISTS meta_enc BYTEA;
ALTER TABLE memory_chunks ADD COLUMN IF NOT EXISTS embed_model_token BYTEA;
ALTER TABLE memory_chunks ADD COLUMN IF NOT EXISTS dims INT;

ALTER TABLE proactive_tasks ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE proactive_tasks ADD COLUMN IF NOT EXISTS label_enc BYTEA;
ALTER TABLE proactive_tasks ADD COLUMN IF NOT EXISTS opportunity_token BYTEA;
ALTER TABLE proactive_tasks ADD COLUMN IF NOT EXISTS day_token BYTEA;
CREATE UNIQUE INDEX IF NOT EXISTS proactive_tasks_token_idx
    ON proactive_tasks (skill, opportunity_token)
    WHERE opportunity_token IS NOT NULL;

ALTER TABLE media_blobs ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE media_blobs ADD COLUMN IF NOT EXISTS meta_enc BYTEA;
ALTER TABLE media_blobs ADD COLUMN IF NOT EXISTS origin_token BYTEA;

ALTER TABLE provider_connections ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE provider_connections ADD COLUMN IF NOT EXISTS account_label_enc BYTEA;
ALTER TABLE provider_connections ADD COLUMN IF NOT EXISTS account_id_enc BYTEA;
ALTER TABLE provider_connections ADD COLUMN IF NOT EXISTS last_error_enc BYTEA;

ALTER TABLE provider_sync_state ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE provider_sync_state ADD COLUMN IF NOT EXISTS value_enc BYTEA;

ALTER TABLE connector_cursors ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE connector_cursors ADD COLUMN IF NOT EXISTS last_seen_enc BYTEA;

ALTER TABLE agent_settings ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE config_overrides ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE oauth_tokens ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE app_secrets ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';
ALTER TABLE workbench_transfers ADD COLUMN IF NOT EXISTS key_version TEXT NOT NULL DEFAULT 'k1';

-- ---------------------------------------------------------------------------
-- New sealed tables. They are empty until the phases that use them land;
-- creating them now keeps one schema audit and one set of AAD bindings.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facts (
    id                   BIGSERIAL PRIMARY KEY,
    subject_identity_id  BIGINT REFERENCES identities(id) ON DELETE CASCADE,
    key_token            BYTEA NOT NULL,
    status               TEXT NOT NULL DEFAULT 'active',
    body_enc             BYTEA NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_version          TEXT NOT NULL DEFAULT 'k1'
);
CREATE INDEX IF NOT EXISTS facts_subject_idx ON facts (subject_identity_id, key_token);

CREATE TABLE IF NOT EXISTS commitments (
    id                      BIGSERIAL PRIMARY KEY,
    kind                    TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'open',
    counterparty_identity_id BIGINT REFERENCES identities(id) ON DELETE SET NULL,
    source_event_id         BIGINT REFERENCES events(id) ON DELETE SET NULL,
    body_enc                BYTEA NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_version             TEXT NOT NULL DEFAULT 'k1'
);
CREATE INDEX IF NOT EXISTS commitments_status_idx ON commitments (status, id);

CREATE TABLE IF NOT EXISTS watches (
    id               BIGSERIAL PRIMARY KEY,
    scope_token      BYTEA NOT NULL,
    channel          TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'active',
    spec_enc         BYTEA NOT NULL,
    last_run_at      TIMESTAMPTZ,
    last_notified_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_version      TEXT NOT NULL DEFAULT 'k1'
);
CREATE INDEX IF NOT EXISTS watches_active_idx ON watches (id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS summaries (
    id           BIGSERIAL PRIMARY KEY,
    kind         TEXT NOT NULL,
    period_token BYTEA NOT NULL,
    body_enc     BYTEA NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_version  TEXT NOT NULL DEFAULT 'k1'
);
CREATE INDEX IF NOT EXISTS summaries_period_idx ON summaries (kind, period_token);

CREATE TABLE IF NOT EXISTS focus (
    chat_ref_token BYTEA PRIMARY KEY,
    state_enc      BYTEA NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_version    TEXT NOT NULL DEFAULT 'k1'
);

CREATE TABLE IF NOT EXISTS conclusions (
    key_token   BYTEA PRIMARY KEY,
    value_enc   BYTEA NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_version TEXT NOT NULL DEFAULT 'k1'
);

CREATE TABLE IF NOT EXISTS deliveries (
    id            BIGSERIAL PRIMARY KEY,
    surface_token BYTEA NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    body_enc      BYTEA NOT NULL,
    message_id_enc BYTEA,
    error_enc     BYTEA,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_version   TEXT NOT NULL DEFAULT 'k1'
);
CREATE INDEX IF NOT EXISTS deliveries_status_idx ON deliveries (status, id);

CREATE TABLE IF NOT EXISTS workspace_files (
    id          BIGSERIAL PRIMARY KEY,
    name_token  BYTEA NOT NULL UNIQUE,
    name_enc    BYTEA NOT NULL,
    content_enc BYTEA NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_version TEXT NOT NULL DEFAULT 'k1'
);