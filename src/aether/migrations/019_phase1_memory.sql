-- Phase 1: write-time enrichment substrate.
--
-- One column, and it is not user-derived: a chunk's position inside its
-- own event. Retrieval needs neighbour expansion (the chunk before and
-- after a hit) and the row ids do not give that — chunks of different
-- events interleave under concurrent ingest, so id ordering is not
-- document ordering. Leaking the order and the count of a document's
-- chunks is the whole of what this exposes; no text, no name, no time.
--
-- Everything else Phase 1 needs already has a sealed home:
--   urgency / deadline / summary / entities  -> events.meta_enc
--   the owner-alias flag                     -> identities.profile_enc
--   the asked-once alias marker              -> entity_notes
--   commitments / conclusions / deliveries   -> tables from 017
-- so no other DDL is required, and no new plaintext column holds anything
-- the user wrote or received.

ALTER TABLE memory_chunks ADD COLUMN IF NOT EXISTS ordinal INT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS memory_chunks_event_ordinal_idx
    ON memory_chunks (event_id, ordinal);
