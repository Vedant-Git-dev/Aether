-- The alert model: one event-based/reminder table, one deterministic
-- notification queue, and the compatibility surfaces a rename needs.
--
-- `watches` becomes `alerts` in place, so every existing row, its sealed
-- spec and its id watermark survive untouched. `routines` becomes
-- `routines_legacy`; `aether.alerts.migrate.routines_to_alerts` reads it
-- once and writes the equivalent alert (with an optional action) because
-- only Python holding the key can open the sealed columns.
--
-- Plaintext added here is the allowlist's shape: a conversation tag, status
-- words, ids, attempt counts and timestamps. The title, criteria, action and
-- provenance are sealed blobs.

DO $$
BEGIN
    IF to_regclass('public.watches') IS NOT NULL
       AND to_regclass('public.alerts') IS NULL THEN
        ALTER TABLE watches RENAME TO alerts;
    END IF;
END $$;

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS origin_enc BYTEA;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS converted_from TEXT;

-- one alert per migrated source row, so a resumed migration cannot duplicate
CREATE UNIQUE INDEX IF NOT EXISTS alerts_converted_idx
    ON alerts (converted_from) WHERE converted_from IS NOT NULL;

-- The old name stays readable for the configured window
-- (alerts.compat_until). It is a read-only projection, never a second store.
CREATE OR REPLACE VIEW watches AS
    SELECT id, scope_token, channel, status, spec_enc, last_run_at,
           last_notified_at, created_at, key_version, last_event_id
    FROM alerts;

DO $$
BEGIN
    IF to_regclass('public.routines') IS NOT NULL
       AND to_regclass('public.routines_legacy') IS NULL THEN
        ALTER TABLE routines RENAME TO routines_legacy;
    END IF;
END $$;

-- One candidate per match: the event snapshot is sealed, the routing facts
-- are plaintext. A repeated match for the same alert and item collides on
-- the keyed token and produces no second row.
CREATE TABLE IF NOT EXISTS alert_notifications (
    id          BIGSERIAL PRIMARY KEY,
    alert_id    BIGINT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    event_id    BIGINT,
    status      TEXT NOT NULL DEFAULT 'queued',
    style       TEXT NOT NULL DEFAULT 'immediate',
    reason      TEXT,
    urgent      INT NOT NULL DEFAULT 0,
    attempts    INT NOT NULL DEFAULT 0,
    next_try_at TIMESTAMPTZ,
    event_enc   BYTEA NOT NULL,
    key_token   BYTEA NOT NULL,
    key_version TEXT NOT NULL DEFAULT 'k1',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS alert_notifications_key_idx
    ON alert_notifications (key_token);
CREATE INDEX IF NOT EXISTS alert_notifications_pending_idx
    ON alert_notifications (status, next_try_at);
CREATE INDEX IF NOT EXISTS alert_notifications_alert_idx
    ON alert_notifications (alert_id, updated_at);
