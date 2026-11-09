-- Notification state: what has already been reported to the user, per
-- event or action. The window that makes a report "recent" lives in config,
-- not here, so changing it changes behavior without a migration.
--
-- Only an opaque blind token, a timestamp and a key version are plaintext.
-- The key text is sealed and bound to the token, so this table cannot leak
-- what the user was told or which action produced it.
CREATE TABLE IF NOT EXISTS notification_reports (
    id           BIGSERIAL PRIMARY KEY,
    key_token    BYTEA NOT NULL UNIQUE,
    body_enc     BYTEA NOT NULL,
    reported_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_version  TEXT NOT NULL DEFAULT 'k1'
);

CREATE INDEX IF NOT EXISTS notification_reports_time_idx
    ON notification_reports (reported_at);
