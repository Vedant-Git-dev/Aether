-- Durable sight of a platform update: long polling's offset lives only
-- in memory, so a restarted process or a network retry re-delivers the
-- same update and the agent answers twice. One row per (source,
-- update_id) is the once-only word; rows age out after a month because
-- no duplicate can still arrive that late.

CREATE TABLE IF NOT EXISTS processed_updates (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,           -- telegram | discord | slack
    update_id   BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, update_id)
);

CREATE INDEX IF NOT EXISTS processed_updates_created_at_idx
    ON processed_updates (created_at);
