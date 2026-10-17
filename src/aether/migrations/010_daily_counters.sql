-- One row per day, counts only — never any content. Shared by the web
-- search cap and the proactive worker's budgets, so the free tiers the
-- deployments ride on (1,000 search credits/month, 2,000 sandbox
-- minutes/month) cannot be burned in a single day.

CREATE TABLE IF NOT EXISTS daily_counters (
    day              TEXT PRIMARY KEY,  -- YYYY-MM-DD, the deployment's local day
    searches         INT NOT NULL DEFAULT 0,
    tasks            INT NOT NULL DEFAULT 0,
    tokens           INT NOT NULL DEFAULT 0,
    sandbox_seconds  INT NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
