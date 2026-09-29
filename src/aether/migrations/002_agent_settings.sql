-- User-editable agent behavior settings — currently just the personality
-- text appended to the system prompt. Single-row table (id is always 1).

CREATE TABLE IF NOT EXISTS agent_settings (
    id              SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    personality_enc BYTEA,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
