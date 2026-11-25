-- Sealed audit entries.
--
-- New entries store the canonical entry as a sealed payload and the chain
-- hash commits to the ciphertext, not to plaintext metadata. Legacy rows
-- keep their plaintext columns and continue to verify with the legacy
-- formula. A row's format is decided by whether payload_enc is present, so
-- no separate flag column is needed and the transition works in place.

ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS payload_enc BYTEA;
