CREATE TABLE IF NOT EXISTS vault_ingress_receipt (
    vault_id BYTEA NOT NULL,
    receipt_token BYTEA NOT NULL CHECK (octet_length(receipt_token) = 32),
    queue_id BYTEA NOT NULL UNIQUE,
    arrival BIGINT GENERATED ALWAYS AS IDENTITY,
    stored_bytes BIGINT NOT NULL CHECK (stored_bytes >= 0),
    state TEXT NOT NULL CHECK (state IN ('queued', 'drained', 'quarantined')),
    PRIMARY KEY (vault_id, receipt_token)
);

CREATE INDEX IF NOT EXISTS vault_ingress_receipt_ready_idx
    ON vault_ingress_receipt (vault_id, arrival) WHERE state = 'queued';
