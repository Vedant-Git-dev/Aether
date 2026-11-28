CREATE TABLE IF NOT EXISTS vault_ingress_notice (
    vault_id BYTEA NOT NULL,
    epoch BIGINT NOT NULL,
    conversation_token BYTEA NOT NULL CHECK (octet_length(conversation_token) = 32),
    queue_id BYTEA NOT NULL REFERENCES vault_ingress_receipt(queue_id),
    state TEXT NOT NULL CHECK (state IN ('unconfirmed', 'sent')),
    PRIMARY KEY (vault_id, epoch, conversation_token)
);
