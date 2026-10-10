// One pending request in plain words: what Aether wants to do, why it stopped,
// what it would change, and the two decisions. No badge — the page title
// already says these are awaiting your approval.
import Icon from "./Icon.jsx";
import { Button } from "./ui.jsx";
import { fmtTime } from "../lib/format.js";
import { describeTool, describeOutcome, describeRule, paramRows } from "../lib/describe.js";

export default function ApprovalCard({ a, ui, onDecide }) {
  const details = paramRows(a.params);
  // why the gate parked this one — the ruling's own reason, in plain words
  const why = describeOutcome(a.note) || describeRule(a.rules_matched);

  return (
    <div data-testid="approval-card" className="border-b border-line p-4 last:border-b-0">
      <div className="flex items-start justify-between gap-3">
        <span data-testid="approval-tool" className="text-sm font-medium text-ink">
          {describeTool(a.tool_name)}
        </span>
        <Icon name="attention" className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
      </div>
      <div className="mt-1 text-xs text-ink-3">
        Requested {fmtTime(a.created_at)} · expires {fmtTime(a.expires_at)}
      </div>
      {why ? <div className="mt-2.5 text-sm text-ink-2">{why}</div> : null}
      {details.length > 0 && (
        <dl className="mt-3 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
          {details.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-ink-3">{k}</dt>
              <dd className="break-words text-ink-2">{v}</dd>
            </div>
          ))}
        </dl>
      )}
      <div className="mt-3.5 flex gap-2">
        <Button kind="approve" data-testid="approve" disabled={ui.pending} onClick={() => onDecide(a.id, "approve")}>
          Approve
        </Button>
        <Button kind="deny" data-testid="deny" disabled={ui.pending} onClick={() => onDecide(a.id, "deny")}>
          Deny
        </Button>
      </div>
      {ui.pending && (
        <div className="mt-3 text-sm text-ink-3">Sending decision…</div>
      )}
      {ui.result && (
        <div
          data-testid="decision-result"
          data-ok={ui.result.ok ? "1" : "0"}
          className={`mt-3 text-sm ${ui.result.ok ? "text-success" : "text-danger"}`}
        >
          {ui.result.msg}
        </div>
      )}
    </div>
  );
}
