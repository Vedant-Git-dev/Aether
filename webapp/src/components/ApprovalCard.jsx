// One pending request, rendered from the request's own shape: labels come
// from its own keys, formats from what the content itself is, long text
// previews and expands to the whole thing in the same layout, an empty
// field says so, and an internal reference points at the decision
// record's exact copy instead of printing its token. What you read here
// is exactly what a decision applies to — nothing above or around it
// repeats the fields or explains the gate. No badge — the page title
// already says these are awaiting your approval.
import { useState } from "react";
import Icon from "./Icon.jsx";
import { Button } from "./ui.jsx";
import { fmtTime } from "../lib/format.js";
import {
  describeTool,
  fieldLabel,
  hasContent,
  ID_REFERRAL,
  isDiff,
  isIdKey,
  isPath,
  isUrl,
  readableDate,
  splitFences,
  structuredValue,
} from "../lib/describe.js";

// where a long value's preview stops — the same leading-lines cut the chat
// question uses, so both faces of a decision preview alike
const PREVIEW_CHARS = 240;
const PREVIEW_LINES = 8;

// whole leading lines while both budgets hold, never a word cut in half —
// and no ellipsis character: the preview reads as the text's own opening,
// and the complete version is one tap away
function previewText(text) {
  const lines = String(text).split("\n");
  const kept = [];
  let total = 0;
  for (const line of lines) {
    if (kept.length >= PREVIEW_LINES || total + line.length > PREVIEW_CHARS) break;
    kept.push(line);
    total += line.length;
  }
  if (kept.length) return kept.join("\n");
  const head = String(text).slice(0, PREVIEW_CHARS);
  const cut = head.lastIndexOf(" ");
  return cut > 0 ? head.slice(0, cut) : head;
}

// long content: the leading lines in the same shapes as the whole thing,
// the size said out loud, one tap to all of it in place — plain text
// throughout, so it stays copyable
function PreviewValue({ text, render }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div data-testid="approval-longtext">
      <div className="break-words text-ink-2">
        {expanded ? render(text) : render(previewText(text))}
      </div>
      <div className="mt-1 flex items-center justify-between gap-2 text-xs text-ink-3">
        <span data-testid="approval-text-size">{text.length.toLocaleString()} characters</span>
        <button
          type="button"
          data-testid="approval-expand"
          className="text-accent hover:underline"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Show less" : "Read full text"}
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
      className="aether-inset overflow-x-auto p-2 font-mono text-xs leading-5"
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
// path, a diff, fenced code beside prose — and a long value previews its
// leading lines and unfolds to the whole thing in the same shapes
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
        className="inline-block break-all rounded-[5px] bg-subtle px-1.5 py-0.5 font-mono text-[13px] text-ink-2"
      >
        {text}
      </code>
    );
  // the whole text decides its shape; the shown part is what flows through
  // it — the preview and the full read never differ in form
  const render = (shown) =>
    isDiff(text) ? (
      <DiffBlock text={shown} />
    ) : (
      splitFences(shown).map((seg, i) =>
        seg.code ? (
          <pre
            key={i}
            data-testid="approval-code"
            className="aether-inset my-1 overflow-x-auto p-2 font-mono text-xs leading-5 text-ink-2"
          >
            {seg.text}
          </pre>
        ) : (
          <div key={i} className="whitespace-pre-wrap text-ink-2">
            {seg.text}
          </div>
        ),
      )
    );
  if (text.length > PREVIEW_CHARS || text.split("\n").length > PREVIEW_LINES)
    return <PreviewValue text={text} render={render} />;
  return <div className="break-words text-ink-2">{render(text)}</div>;
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
              {isIdKey(k) && hasContent(v) ? (
                <span data-testid="approval-reference" className="text-ink-3">
                  {ID_REFERRAL}
                </span>
              ) : (
                <Value value={v} />
              )}
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
  const entries = Object.entries(a.params || {}).filter(([k]) => !k.startsWith("_"));

  return (
    <div data-testid="approval-card" className="border-b border-[var(--aether-hairline)] p-4 last:border-b-0 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <span data-testid="approval-tool" className="text-sm font-medium text-ink">
          {describeTool(a.tool_name)}
        </span>
        <Icon name="attention" className="mt-0.5 h-4 w-4 shrink-0 text-warning" />
      </div>
      <div className="mt-2 text-xs text-ink-3">Expires {fmtTime(a.expires_at)}</div>
      {/* the ask itself — its own fields in their own order, a long value
          previewing with its size said and the full read one tap away; the
          height cap keeps the decision buttons on screen even unfolded */}
      <div data-testid="approval-details" className="mt-3 max-h-96 overflow-y-auto">
        {entries.length ? (
          <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 text-sm">
            {entries.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="whitespace-nowrap text-ink-3">{fieldLabel(k)}</dt>
                <dd className="min-w-0">
                  {isIdKey(k) && hasContent(v) ? (
                    <span data-testid="approval-reference" className="text-ink-3">
                      {ID_REFERRAL}
                    </span>
                  ) : (
                    <Value value={v} />
                  )}
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
