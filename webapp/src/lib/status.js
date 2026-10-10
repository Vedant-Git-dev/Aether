// Status tokens -> plain text colour. There are no chips, dots or filled
// badges anywhere in this panel: colour carries meaning in text only, and
// every class name is a full literal so the Tailwind scanner sees it.
const COLORS = {
  success: "text-success",
  warning: "text-warning",
  danger: "text-danger",
  muted: "text-ink-3",
};

const STATUS_COLORS = {
  ok: COLORS.success, allow: COLORS.success, approved: COLORS.success,
  executed: COLORS.success, done: COLORS.success, completed: COLORS.success,
  approve: COLORS.warning, require_approval: COLORS.warning,
  waiting: COLORS.warning, pending: COLORS.warning, warn: COLORS.warning,
  deny: COLORS.danger, denied: COLORS.danger, failed: COLORS.danger,
  blocked: COLORS.danger, filtered: COLORS.danger, error: COLORS.danger, bad: COLORS.danger,
  active: COLORS.muted, running: COLORS.muted, processing: COLORS.muted,
  info: COLORS.muted, idle: COLORS.muted, expired: COLORS.muted, disabled: COLORS.muted,
};

export function statusColor(status) {
  return STATUS_COLORS[String(status || "").toLowerCase()] || COLORS.muted;
}
