-- One-time transfer URLs for the batch workbench: the job bundle a
-- batch runner pulls, and the result it pushes back. The token itself
-- never lives here — only its sha256 — and the payloads (a job's files,
-- a job's output) are AES-GCM blobs bound to the token hash, the same
-- pattern every other store uses. A row is single-use: the bundle read
-- or the result write marks it used, and nothing serves it again.

CREATE TABLE IF NOT EXISTS workbench_transfers (
    id          BIGSERIAL PRIMARY KEY,
    token_hash  BYTEA NOT NULL UNIQUE,  -- sha256 of the 32-byte url token
    kind        TEXT NOT NULL,          -- 'bundle' | 'result'
    payload_enc BYTEA NOT NULL,         -- the job's files, or its posted result
    used        BOOLEAN NOT NULL DEFAULT false,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS workbench_transfers_expires_idx ON workbench_transfers (expires_at);

-- Wall-clock minutes a batch queue billed this month — counts only, the
-- same class as daily_counters: no content, no job names, plain columns.
-- One row per local month; a month with no batch work has no row.

CREATE TABLE IF NOT EXISTS workbench_minutes (
    month      TEXT PRIMARY KEY,  -- YYYY-MM, the deployment's local month
    seconds    INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
