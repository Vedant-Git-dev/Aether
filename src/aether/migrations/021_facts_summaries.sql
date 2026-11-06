-- The facts layer and summaries (plan.md section 7).
--
-- Both tables already exist from 017_sealed_storage.sql: a `facts` row keeps
-- a plaintext subject id, a keyed blind token over (subject, key), a status
-- word and a sealed body; a `summaries` row keeps a kind word, a keyed blind
-- token over the period, and a sealed body. Nothing new holds anything the
-- user wrote or received, so this migration adds only the indexes the
-- consolidation upserts need.
--
-- One active fact per (subject, key): a partial unique index over the
-- coalesced subject (a subjectless fact uses 0) so a concurrent writer can
-- never leave two active claims for the same key. Superseded and disputed
-- rows are exempt — the history is kept, never overwritten.
CREATE UNIQUE INDEX IF NOT EXISTS facts_active_key_idx
    ON facts (COALESCE(subject_identity_id, 0), key_token)
    WHERE status = 'active';

-- One row per (kind, period): the summarizer upserts in place.
CREATE UNIQUE INDEX IF NOT EXISTS summaries_period_unique_idx
    ON summaries (kind, period_token);
