-- Sealed ingress queue, queue parts and the operator migration journal.
--
-- The queue id is reserved before sealing so a crash cannot let a later item
-- reuse it. A group is complete only when its end record and every part are
-- present. The journal records opaque stable references and state.

CREATE TABLE IF NOT EXISTS vault_ingress_queue (
    queue_id    BYTEA PRIMARY KEY,
    vault_id    BYTEA NOT NULL,
    ingress_ver BIGINT NOT NULL CHECK (ingress_ver >= 1),
    ciphertext_enc BYTEA NOT NULL DEFAULT ''::bytea,
    complete    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS vault_ingress_queue_pending_idx
    ON vault_ingress_queue (vault_id, ingress_ver, created_at) WHERE NOT complete;

CREATE TABLE IF NOT EXISTS vault_ingress_part (
    queue_id   BYTEA NOT NULL REFERENCES vault_ingress_queue(queue_id) ON DELETE CASCADE,
    part       INTEGER NOT NULL CHECK (part >= 0),
    kind       TEXT NOT NULL CHECK (kind IN ('chunk', 'end')),
    ciphertext_enc BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (queue_id, part)
);

CREATE TABLE IF NOT EXISTS vault_migration_journal (
    job_id     BYTEA NOT NULL,
    vault_id   BYTEA NOT NULL,
    table_name TEXT NOT NULL,
    stable_id  BYTEA NOT NULL CHECK (octet_length(stable_id) = 32),
    state      TEXT NOT NULL CHECK (state IN ('pending', 'done', 'failed')),
    detail_enc BYTEA,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (vault_id, job_id, table_name, stable_id)
);

CREATE INDEX IF NOT EXISTS vault_migration_journal_state_idx
    ON vault_migration_journal (job_id, state);
