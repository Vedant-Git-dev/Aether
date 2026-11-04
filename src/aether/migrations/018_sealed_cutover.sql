-- Sealed storage, cutover phase.
--
-- `017_sealed_storage.sql` added the sealed copies and the sealer filled
-- them. This migration removes the plaintext originals. It is deliberately
-- defensive: if a single row still lacks its sealed copy it raises before
-- dropping anything, because the alternative is deleting history.
--
-- On a deploy the boot seals first and then applies this (see
-- `memory/db.py:run_migrations` and `memory/seal_migrate.py`);
-- `python -m aether.memory.seal_migrate --verify-only` reports the same
-- state by hand.

DO $$
DECLARE
    pending BIGINT;
BEGIN
    SELECT COUNT(*) INTO pending FROM events
      WHERE meta_enc IS NULL OR source_token IS NULL OR dedup_mac IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % event row(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM identities WHERE profile_enc IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % identity row(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM identity_handles
      WHERE handle_enc IS NULL OR platform_token IS NULL OR normalized_token IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % handle row(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM chat_messages WHERE surface_token IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % chat row(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM pending_approvals
      WHERE rules_matched_enc IS NULL OR note_enc IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % approval row(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM scheduled_actions WHERE meta_enc IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % scheduled action(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM routines WHERE label_enc IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % routine(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM decision_traces WHERE label_enc IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % trace(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM memory_chunks
      WHERE meta_enc IS NULL OR embed_model_token IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % chunk row(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM proactive_tasks
      WHERE label_enc IS NULL OR meta_enc IS NULL
         OR opportunity_token IS NULL OR day_token IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % proactive task(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM media_blobs
      WHERE meta_enc IS NULL OR origin_token IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % media row(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM provider_connections
      WHERE account_label_enc IS NULL OR account_id_enc IS NULL OR last_error_enc IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % provider connection(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM provider_sync_state WHERE value_enc IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % sync cursor row(s) are not sealed', pending;
    END IF;

    SELECT COUNT(*) INTO pending FROM connector_cursors WHERE last_seen_enc IS NULL;
    IF pending > 0 THEN
        RAISE EXCEPTION 'sealed cutover refused: % connector cursor(s) are not sealed', pending;
    END IF;
END $$;

-- events: source, kind, time, salience and the old structural hash and
-- JSON metadata all live in meta_enc; dedup is the keyed tag pair.
ALTER TABLE events
    DROP COLUMN IF EXISTS source,
    DROP COLUMN IF EXISTS kind,
    DROP COLUMN IF EXISTS occurred_at,
    DROP COLUMN IF EXISTS content_hash,
    DROP COLUMN IF EXISTS salience_score,
    DROP COLUMN IF EXISTS memorable,
    DROP COLUMN IF EXISTS meta;

ALTER TABLE identities
    DROP COLUMN IF EXISTS display_name,
    DROP COLUMN IF EXISTS confidence;

ALTER TABLE identity_handles
    DROP COLUMN IF EXISTS platform,
    DROP COLUMN IF EXISTS raw_handle,
    DROP COLUMN IF EXISTS normalized;

ALTER TABLE chat_messages
    DROP COLUMN IF EXISTS surface;

ALTER TABLE pending_approvals
    DROP COLUMN IF EXISTS rules_matched,
    DROP COLUMN IF EXISTS note;

ALTER TABLE scheduled_actions
    DROP COLUMN IF EXISTS label,
    DROP COLUMN IF EXISTS run_at;

ALTER TABLE routines
    DROP COLUMN IF EXISTS label;

ALTER TABLE decision_traces
    DROP COLUMN IF EXISTS label;

ALTER TABLE memory_chunks
    DROP COLUMN IF EXISTS source,
    DROP COLUMN IF EXISTS kind,
    DROP COLUMN IF EXISTS filename,
    DROP COLUMN IF EXISTS location,
    DROP COLUMN IF EXISTS occurred_at,
    DROP COLUMN IF EXISTS embed_model,
    DROP COLUMN IF EXISTS embedding,
    DROP COLUMN IF EXISTS norm;

ALTER TABLE proactive_tasks
    DROP COLUMN IF EXISTS label,
    DROP COLUMN IF EXISTS opportunity_key,
    DROP COLUMN IF EXISTS day;

ALTER TABLE media_blobs
    DROP COLUMN IF EXISTS filename,
    DROP COLUMN IF EXISTS media_type,
    DROP COLUMN IF EXISTS width,
    DROP COLUMN IF EXISTS height,
    DROP COLUMN IF EXISTS duration_s,
    DROP COLUMN IF EXISTS origin;

ALTER TABLE provider_connections
    DROP COLUMN IF EXISTS account_label,
    DROP COLUMN IF EXISTS account_id,
    DROP COLUMN IF EXISTS last_error;

ALTER TABLE provider_sync_state
    DROP COLUMN IF EXISTS value;

ALTER TABLE connector_cursors
    DROP COLUMN IF EXISTS last_seen;
