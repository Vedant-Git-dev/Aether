CREATE TABLE IF NOT EXISTS held_notes (
    id          BIGSERIAL PRIMARY KEY,
    body_enc    BYTEA NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_version TEXT NOT NULL DEFAULT 'k1'
);
