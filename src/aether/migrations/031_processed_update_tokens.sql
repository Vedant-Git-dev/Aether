-- Scoped connector receipts keep a category-keyed deduplication token.
-- The existing writer keeps its original representation until cutover.
ALTER TABLE processed_updates ALTER COLUMN update_id DROP NOT NULL;
ALTER TABLE processed_updates ADD COLUMN IF NOT EXISTS dedup_token BYTEA;
ALTER TABLE processed_updates ADD CONSTRAINT processed_updates_one_identity
    CHECK ((update_id IS NULL) <> (dedup_token IS NULL));
CREATE UNIQUE INDEX IF NOT EXISTS processed_updates_dedup_token_idx
    ON processed_updates (dedup_token) WHERE dedup_token IS NOT NULL;
