-- Forgetting, and the tombstone that makes it stick (plan.md section 12.12).
--
-- Deleting an event row is not enough: ingest dedups on (source_token,
-- dedup_mac), so the next poll would store the same message again. A
-- forgotten event leaves its two keyed blind tokens here, and every later
-- ingest with the same mac is refused before anything is written.
--
-- Nothing user-derived is stored: both columns are HMAC tokens and the
-- timestamp is bookkeeping.
CREATE TABLE IF NOT EXISTS forgotten_events (
    dedup_mac    BYTEA PRIMARY KEY,
    source_token BYTEA NOT NULL,
    forgotten_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
