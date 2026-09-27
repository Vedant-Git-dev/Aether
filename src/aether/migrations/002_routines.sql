-- Routines: standing event triggers — "when X happens, do Y".
-- The trigger spec and the action payload are human-readable content, so
-- both are encrypted at rest; everything plaintext is structure.

CREATE TABLE IF NOT EXISTS routines (
    id               BIGSERIAL PRIMARY KEY,
    label            TEXT        NOT NULL,
    trigger_enc      BYTEA       NOT NULL,
    action_enc       BYTEA       NOT NULL,
    enabled          BOOLEAN     NOT NULL DEFAULT TRUE,
    cooldown_seconds INT         NOT NULL DEFAULT 300,
    fire_count       BIGINT      NOT NULL DEFAULT 0,
    last_fired_at    TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS routines_enabled_idx
    ON routines (id) WHERE enabled;
