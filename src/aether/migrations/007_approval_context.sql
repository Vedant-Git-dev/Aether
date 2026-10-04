-- Approval context: why a call is parked, carried on the approval itself.
--
-- Approvals.create() always knew the gate's ruling (rules_matched) and its
-- plain reason (note) — but only wrote them to the audit row, so the web
-- panel's attention queue could show what was asked, never why. These two
-- columns keep the reason on the approval row itself, where the panel's
-- /api/approvals feed can serve it next to the params.

ALTER TABLE pending_approvals
    ADD COLUMN IF NOT EXISTS rules_matched TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS note          TEXT NOT NULL DEFAULT '';
