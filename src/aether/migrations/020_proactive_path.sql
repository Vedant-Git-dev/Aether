-- The proactive path: watches that fire on inbound events, and the ledger
-- that keeps commitment reminders from repeating after a restart.
--
-- Watches already have a sealed spec and plain routing metadata (017). The
-- one missing fact is how far a watch has evaluated: an id watermark, not a
-- timestamp, because ingest order is the order the evaluator sees and a
-- mail dated slightly earlier than the last one polled must not be skipped.
-- An event id is opaque.
ALTER TABLE watches ADD COLUMN IF NOT EXISTS last_event_id BIGINT NOT NULL DEFAULT 0;

-- One row per reminder actually sent. lead_minutes is the lead that fired
-- (1440, 120, 15) or -1 for the single overdue escalation. Ids and integer
-- minutes only: nothing the user wrote lives here.
CREATE TABLE IF NOT EXISTS commitment_reminders (
    id             BIGSERIAL PRIMARY KEY,
    commitment_id  BIGINT NOT NULL REFERENCES commitments(id) ON DELETE CASCADE,
    lead_minutes   INT NOT NULL,
    sent_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (commitment_id, lead_minutes)
);
CREATE INDEX IF NOT EXISTS commitment_reminders_commitment_idx
    ON commitment_reminders (commitment_id);
