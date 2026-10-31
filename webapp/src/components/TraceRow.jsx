// One trace: a summary head (click to expand) and, when open, the lazy-fetched
// detail. The kind is plain muted text; the caret just flips orientation.
import Icon from "./Icon.jsx";
import { fmtTime } from "../lib/format.js";
import { TRACE_KINDS, describeTool, humanize } from "../lib/describe.js";
import TraceDetail from "./TraceDetail.jsx";

// The backend writes carry_out labels as "approval #N — <raw tool name>";
// the raw name never renders. Turn/routine/scheduled labels are already the
// plain text of the message, routine or action.
function traceLabel(t) {
  if (t.kind === "carry_out") {
    const m = /^approval #(\d+) — (.+)$/.exec(t.label || "");
    return m ? `Approval #${m[1]} — ${describeTool(m[2])}` : describeTool(t.label);
  }
  return t.label || "(no label)";
}

export default function TraceRow({ t, open, detail, onToggle }) {
  return (
    <div data-testid="trace-row" data-open={open ? "1" : "0"} className="border-b border-[var(--aether-hairline)] last:border-b-0">
      <button
        type="button"
        data-testid="trace-head"
        onClick={() => onToggle(t.id)}
        aria-expanded={open ? "true" : "false"}
        className={`flex w-full items-center gap-3 px-4 py-3 text-left transition-colors ${
          open ? "bg-subtle/45" : "hover:bg-subtle/60"
        }`}
      >
        <span className="w-[86px] shrink-0 whitespace-nowrap font-mono text-[10.5px] text-ink-3">{fmtTime(t.at)}</span>
        <span className="shrink-0 text-[11.5px] text-ink-3">{TRACE_KINDS[t.kind] || humanize(t.kind)}</span>
        <span className="min-w-0 flex-1 truncate text-sm text-ink">{traceLabel(t)}</span>
        <Icon name="arrow" className={`h-3.5 w-3.5 shrink-0 text-ink-3 transition-transform ${open ? "rotate-90" : ""}`} />
      </button>
      {open && (
        <div className="border-t border-[var(--aether-hairline)] bg-subtle/30 px-4 py-4">
          {detail ? <TraceDetail t={detail} /> : <div className="text-[13px] text-ink-3">Loading…</div>}
        </div>
      )}
    </div>
  );
}
