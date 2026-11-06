-- The inbound ledger: one terminal outcome per message a person sent.
--
-- Without it an unprompted tick cannot tell a delivered answer from one
-- that never went out, so a stale question gets answered again. A row is
-- 'received' on arrival, then 'answered' (the outbound id recorded),
-- 'failed' (reason sealed) or 'expired'.
--
-- Plaintext here is only the surface vocabulary, the status word, a
-- boolean, timestamps and keyed blind tokens for equality lookup; the
-- failure reason and the outbound message id are sealed.
CREATE TABLE IF NOT EXISTS inbound_messages (
    id            BIGSERIAL PRIMARY KEY,
    surface       TEXT NOT NULL,
    chat_token    BYTEA NOT NULL,
    message_token BYTEA NOT NULL,
    key_token     BYTEA NOT NULL,
    status        TEXT NOT NULL DEFAULT 'received',
    checked       BOOLEAN NOT NULL DEFAULT false,
    reason_enc    BYTEA,
    outcome_enc   BYTEA,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_version   TEXT NOT NULL DEFAULT 'k1'
);

-- one row per platform message (or the caller's fallback key): a
-- re-delivered update collides here and produces no second turn
CREATE UNIQUE INDEX IF NOT EXISTS inbound_messages_key_idx
    ON inbound_messages (key_token);

CREATE INDEX IF NOT EXISTS inbound_messages_status_idx
    ON inbound_messages (status, created_at);

CREATE INDEX IF NOT EXISTS inbound_messages_chat_idx
    ON inbound_messages (chat_token, created_at);
