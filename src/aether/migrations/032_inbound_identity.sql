-- Preserve the exact inputs needed to rotate inbound lookup tokens.
ALTER TABLE inbound_messages ADD COLUMN IF NOT EXISTS identity_enc BYTEA;
