-- Config overrides: the changes made to config.yaml's settings from chat, one
-- row per setting path ("agent.tick_seconds", "contacts.allowlist", ...).
-- On Render's ephemeral disk the yaml file is wiped on every deploy, so the
-- database is the only durable home for a change made from chat. Values are
-- encrypted (an MCP transport block can carry env secrets) and bound to
-- their path through the cipher's AAD, so a blob can't be swapped between
-- rows. The yaml file (or the defaults, where no file exists) stays the boot
-- base — these rows merge on top of it at boot, and a reset deletes the row,
-- falling back to whatever the file says.

CREATE TABLE IF NOT EXISTS config_overrides (
    path        TEXT PRIMARY KEY,
    value_enc   BYTEA,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
