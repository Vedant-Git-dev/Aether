-- One more plain count on the day's row: how many proactive result
-- notifications went out. The notify_daily_cap reads it — the same
-- no-content trade the other counters already make.

ALTER TABLE daily_counters
    ADD COLUMN IF NOT EXISTS notifications INT NOT NULL DEFAULT 0;
