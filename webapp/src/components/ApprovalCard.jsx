// One pending request, rendered from the request's own shape: labels come
// from its own keys, formats from what the content itself is, long text
// scrolls and expands instead of cutting, and an empty field says so.
// What you read here is exactly what a decision applies to. No badge — the
// page title already says these are awaiting your approval.
import { useState } from "react";
import Icon from "./Icon.jsx";
import { Button } from "./ui.jsx";
import { fmtTime } from "../lib/format.js";
import {
  approvalSummary,
  describeOutcome,
  describeRule,
  describeTool,
  fieldLabel,
  isDiff,
  isPath,
  isUrl,
  readableDate,
  splitFences,
  structuredValue,
} from "../lib/describe.js";

// a value only becomes a scroll box past these — under them it renders in
// full with no controls in the way
const LONG_CHARS = 600;
const LONG_LINES = 12;

// long content: capped to a scroll box that says its size out loud, one
// click to all of it — plain text throughout, so it stays copyable
function ScrollableValue({ text, children }) {
  const [expanded, setExpanded] = useState(false);
  if (text.length <= LONG_CHARS && text.split("\n").length <= LONG_LINES) {
    return <div className="break-words text-ink-2">{children}</div>;
  }
  return (
    <div data-testid="approval-longtext">
      <div className={`overflow-y-auto ${expanded ? "" : "max-h-64"}`}>{children}</div>
      <div className="mt-1 flex items-center justify-between gap-2 text-xs text-ink-3">
        <span data-testid="approval-text-size">{text.length.toLocaleString()} characters</span>
        <button
          type="button"
          data-testid="approval-expand"
          className="text-accent hover:underline"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Show less" : "Expand"}
        </button>
      </div>
    </div>
  );
}

// a unified diff keeps its shape: monospace, added and removed lines
// coloured, nothing reflowed
function DiffBlock({ text }) {
  return (
    <pre
      data-testid="approval-diff"
      className="overflow-x-auto rounded-md bg-subtle p-2 font-mono text-xs leading-5"
    >
      {text.split("\n").map((line, i) => (
        <span
          key={i}
          className={
            line.startsWith("+")
              ? "text-success"
              : line.startsWith("-")
                ? "text-danger"
                : line.startsWith("@@")
                  ? "text-ink-3"
                  : "text-ink-2"
          }
        >
          {line}
          {"\n"}
        </span>
      ))}
    </pre>
  );
}

// a text value: its own shape decides how it reads — a date, a link, a
// path, a diff, fenced code beside prose — and anything unrecognized falls
// back to plain readable text, never dropped or cut
function TextValue({ text }) {
  const when = readableDate(text);
  if (when) return <span className="break-words text-ink-2">{when}</span>;
  if (isUrl(text))
    return (
      <a href={text} target="_blank" rel="noreferrer" className="break-all text-accent hover:underline">
        {text}
      </a>
    );
  if (isPath(text))
    return (
      <code
        data-testid="approval-path"
        className="inline-block break-all rounded bg-subtle px-1.5 py-0.5 font-mono text-[13px] text-ink-2"
      >
        {text}
      </code>
    );
  if (isDiff(text))
    return (
      <ScrollableValue text={text}>
        <DiffBlock text={text} />
      </ScrollableValue>
    );
  return (
    <ScrollableValue text={text}>
      {splitFences(text).map((seg, i) =>
        seg.code ? (
          <pre
            key={i}
            data-testid="approval-code"
            className="my-1 overflow-x-auto rounded-md bg-subtle p-2 font-mono text-xs leading-5 text-ink-2"
          >
            {seg.text}
          </pre>
        ) : (
          <div key={i} className="whitespace-pre-wrap text-ink-2">
            {seg.text}
          </div>
        ),
      )}
    </ScrollableValue>
  );
}

// any value whatsoever, by what it is: a serialized structure unpacks,
// arrays number their items, objects label their own fields in their own
// order, and empties say so — nothing is ever skipped
function Value({ value }) {
  const structured = structuredValue(value);
  if (structured) return <Value value={structured} />;
  if (value === null || value === undefined || value === "")
    return <span className="text-ink-3">(empty)</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="text-ink-3">(empty)</span>;
    return (
      <ol data-testid="approval-items" className="list-decimal list-outside space-y-1.5 pl-5">
        {value.map((item, i) => (
          <li key={i} className="min-w-0">
            <Value value={item} />
          </li>
        ))}
      </ol>
    );
  }
  if (typeof value === "object") {
    const entries = Object.entries(value).filter(([k]) => !k.startsWith("_"));
    if (!entries.length) return <span className="text-ink-3">(empty)</span>;
    return (
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
        {entries.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="whitespace-nowrap text-ink-3">{fieldLabel(k)}</dt>
            <dd className="min-w-0">
              <Value value={v} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  if (typeof value === "boolean")
    return <span className="text-ink-2">{value ? "yes" : "no"}</span>;
  return <TextValue text={String(value)} />;
}

export default function ApprovalCard({ a, ui, onDecide }) {
  // why the gate parked this one — the ruling's own reason, in plain words
  const why = describeOutcome(a.note) || describeRule(a.rules_matched);
  const summary = approvalSummary(a.tool_name, a.params);
  const entries = Object.entries(a.params || {}).filter(([k]) => !k.startsWith("_"));

  return (
    <div data-testid="approval-card" className="border-b border-line p-4 last:border-b-0">
      <div className="flex items-start justify-between gap-3">
        <span data-testid="approval-tool" className="text-sm font-medium text-ink">
          {describeTool(a.tool_name)}
        </span>
        <Icon name="attention" className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
      </div>
      <div data-testid="approval-summary" className="mt-2 text-sm text-ink-2">
        {summary}
      </div>
      {why ? (
        <div data-testid="approval-why" className="mt-2 text-sm text-warning">
          {why}
        </div>
      ) : null}
      <div className="mt-1 text-xs text-ink-3">
        Requested {fmtTime(a.created_at)} · expires {fmtTime(a.expires_at)}
      </div>
      {/* the ask itself, capped so the decision buttons never leave the
          screen — anything taller scrolls inside, and long values carry
          their own size indicator and expand control */}
      <div data-testid="approval-details" className="mt-3 max-h-96 overflow-y-auto">
        {entries.length ? (
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 text-sm">
            {entries.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="whitespace-nowrap text-ink-3">{fieldLabel(k)}</dt>
                <dd className="min-w-0">
                  <Value value={v} />
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <span className="text-sm text-ink-3">(empty)</span>
        )}
      </div>
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
