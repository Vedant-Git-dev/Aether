-- Encrypted media blobs: shared images, voice notes, videos, and the
-- files Aether makes itself. Content is one AES-256-GCM blob bound to
-- its row the same way event payloads are; the plain columns are
-- routing metadata only. Dedup is content-addressed but keyed -- the
-- stored tag is an HMAC over an HKDF subkey of the encryption secret,
-- so a database reader cannot confirm "this exact known file is
-- stored here" the way a bare sha256 would let them.

CREATE TABLE IF NOT EXISTS media_blobs (
    id           BIGSERIAL PRIMARY KEY,
    filename     TEXT NOT NULL DEFAULT '',
    media_type   TEXT NOT NULL,          -- sniffed from magic bytes, never the declared MIME
    size         INT  NOT NULL,         -- bytes
    width        INT,
    height       INT,
    duration_s   REAL,
    origin       TEXT NOT NULL,         -- telegram | discord | slack | web | screen | generated | workbench
    dedup_mac     BYTEA NOT NULL,
    bytes_enc    BYTEA NOT NULL,        -- nonce(12B) || ciphertext+tag, AAD media:bytes_enc:{id}
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS media_blobs_dedup_idx ON media_blobs (dedup_mac);
CREATE INDEX IF NOT EXISTS media_blobs_lru_idx ON media_blobs (last_used_at);

-- two more plain counts on the day's row: generated images, and the
-- sandbox jobs that read or edit media (frame extraction, video
-- edits) -- the same no-content trade the other counters make.
ALTER TABLE daily_counters
    ADD COLUMN IF NOT EXISTS images INT NOT NULL DEFAULT 0;
ALTER TABLE daily_counters
    ADD COLUMN IF NOT EXISTS sandbox_jobs INT NOT NULL DEFAULT 0;

-- explicit media references: an approval that would send a file, a
-- task result that carries one. Nullable and indexed; written only by
-- the code that parks or stores media. Eviction's protected-set
-- query filters by status, so a missed clear or a lazy expiry can
-- never strand a blob or starve the quota.
ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS media_id BIGINT;
CREATE INDEX IF NOT EXISTS pending_approvals_media_idx ON pending_approvals (media_id);
ALTER TABLE proactive_tasks ADD COLUMN IF NOT EXISTS media_id BIGINT;
CREATE INDEX IF NOT EXISTS proactive_tasks_media_idx ON proactive_tasks (media_id);
