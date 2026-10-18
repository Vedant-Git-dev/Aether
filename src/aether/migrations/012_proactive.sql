-- The proactive worker's task rows: one row per opportunity ever taken.
-- The unique key below is the dedup — the same email thread, the same
-- calendar slot, the same file can only ever become one task, so a
-- discarded key never re-detects and burns budget again. Everything with
-- content (what was detected, the plan, the result, the feedback note) is
-- an AES-GCM blob bound to its row; the plain columns are routing
-- metadata only — which skill, what label, what state, which provider,
-- which local day it was claimed on.

CREATE TABLE IF NOT EXISTS proactive_tasks (
    id               BIGSERIAL PRIMARY KEY,
    skill            TEXT NOT NULL,
    label            TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    opportunity_key  TEXT NOT NULL,          -- stable: skill + source ids
    opportunity_enc  BYTEA NOT NULL,         -- the detail the plan re-reads
    plan_enc         BYTEA,                  -- what the skill decided to do
    result_enc       BYTEA,                  -- the plain-words outcome
    feedback         TEXT,                   -- approved | revised | discarded
    feedback_note_enc BYTEA,                 -- the user's optional reason
    provider         TEXT,                   -- which sandbox ran it, if any
    day              TEXT NOT NULL,          -- YYYY-MM-DD, the day it was claimed
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- the worker's queue: pending rows only, oldest first
CREATE INDEX IF NOT EXISTS proactive_tasks_pending_idx
    ON proactive_tasks (id) WHERE status = 'pending';

-- the dedup: one task per (skill, opportunity), ever
CREATE UNIQUE INDEX IF NOT EXISTS proactive_tasks_skill_opportunity_idx
    ON proactive_tasks (skill, opportunity_key);
