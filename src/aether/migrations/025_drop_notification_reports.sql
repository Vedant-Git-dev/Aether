-- Drop the notification-report table. The notifications gate that read and
-- wrote it is gone: a proactive note is always offered, and quiet hours hold
-- and batch it instead of dropping it. Nothing reads or writes this table now.
DROP TABLE IF EXISTS notification_reports;
