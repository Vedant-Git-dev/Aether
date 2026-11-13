-- Roll back the alert model. Data written before the migration is intact in
-- the renamed tables; rows written after it stay in `watches` (the old
-- schema tolerates the extra sealed spec keys) and are not converted back
-- into routines, because a routine's identity was its trigger/action pair
-- and the alert row no longer says which of its fields came from where.
--
-- Run with: python -c "from aether.memory.db import ...; rollback_migration(...)"
-- or apply this file directly, then delete the schema_migrations row.

DROP VIEW IF EXISTS watches;

DROP TABLE IF EXISTS alert_notifications;

DO $$
BEGIN
    IF to_regclass('public.alerts') IS NOT NULL
       AND to_regclass('public.watches') IS NULL THEN
        ALTER TABLE alerts RENAME TO watches;
    END IF;
END $$;

ALTER TABLE watches DROP COLUMN IF EXISTS origin_enc;
ALTER TABLE watches DROP COLUMN IF EXISTS converted_from;
DROP INDEX IF EXISTS alerts_converted_idx;

DO $$
BEGIN
    IF to_regclass('public.routines_legacy') IS NOT NULL
       AND to_regclass('public.routines') IS NULL THEN
        ALTER TABLE routines_legacy RENAME TO routines;
    END IF;
END $$;

DELETE FROM schema_migrations WHERE version = '026_alerts';
