-- App secrets: keys pasted in chat during an /apps walk, one row per name.
-- The paste is consumed before ingest — no model, no memory, no trace —
-- and lands here encrypted with the same AES-256-GCM key as all other
-- content, bound to its row through the cipher's AAD. The resolver reads
-- this table before .env (a paste is the most recent deliberate act), so
-- an MCP server's config keeps referencing keys by name only ($NAME),
-- exactly like a key that lives in .env. Values are never echoed back,
-- never written to config.yaml, and never recorded in the audit log —
-- the audit row for a paste carries the name only.

CREATE TABLE IF NOT EXISTS app_secrets (
    name       TEXT PRIMARY KEY,
    value_enc  BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
