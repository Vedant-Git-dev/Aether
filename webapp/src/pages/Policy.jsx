// The ladder: your rules first, then the standard rules beneath, each with
// its plain-language decision on the right. Borders only — no fills, no chips.
import { useEffect, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Card } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import { useData } from "../store.jsx";
import { hasToken } from "../lib/api.js";
import { describeToolPattern, humanize, DECISION_LABELS, BUILTIN_PLAIN } from "../lib/describe.js";
import { statusColor } from "../lib/status.js";

export default function PolicyPage() {
  const { ensurePolicy } = useData();
  const [policy, setPolicy] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!hasToken()) return;
    let alive = true;
    ensurePolicy()
      .then((d) => alive && setPolicy(d))
      .catch((e) => alive && setError(e.message));
    return () => { alive = false; };
  }, [ensurePolicy]);

  return (
    <div>
      <PageHead
        eyebrow="Read only"
        title="Authorization & Policies"
        sub="What Aether may do on its own, and what always requires your approval."
      />
      {!hasToken() ? (
        <Card><NoToken /></Card>
      ) : error ? (
        <Card><EmptyState title="Could not load the policy." sub={error} /></Card>
      ) : !policy ? (
        <Card><div className="px-4 py-6 text-sm text-ink-3">Loading…</div></Card>
      ) : (
        <>
          <Card className="mb-4 p-4">
            <h3 className="mb-3 text-sm font-semibold text-ink">Your rules — checked first</h3>
            {policy.rules.length === 0 ? (
              <EmptyState title="No custom rules yet." sub="Aether follows the standard rules below." />
            ) : (
              policy.rules.map((r, i) => (
                <div key={i} className="flex items-baseline gap-3 border-b border-line py-2.5 last:border-b-0">
                  <span className="w-5 shrink-0 text-right font-mono text-xs text-ink-3">{i + 1}</span>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm text-ink">
                      {describeToolPattern(r.tool_pattern)}
                      {r.param_pattern && <span className="text-ink-3"> · only in certain cases</span>}
                    </div>
                    {r.note && <div className="mt-0.5 text-xs text-ink-3">{r.note}</div>}
                  </div>
                  <span className={`shrink-0 text-xs font-medium ${statusColor(r.decision)}`}>
                    {DECISION_LABELS[r.decision] || humanize(r.decision)}
                  </span>
                </div>
              ))
            )}
          </Card>

          <Card className="p-4">
            <h3 className="mb-3 text-sm font-semibold text-ink">Standard rules</h3>
            {policy.builtin_rules.map((r, i) => {
              const [title, description] = BUILTIN_PLAIN[r.id]
                || [humanize(String(r.id).replace(/^(builtin|default):/, "")), r.description];
              const label = DECISION_LABELS[r.decision] || humanize(r.decision);
              return (
                <div key={r.id} className="flex items-baseline gap-3 border-b border-line py-2.5 last:border-b-0">
                  <span className="w-5 shrink-0 text-right font-mono text-xs text-ink-3">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-ink">
                      {title} — <span className="font-normal">{label.toLowerCase()}</span>
                    </div>
                    <div className="mt-0.5 text-xs text-ink-3">{description}</div>
                  </div>
                </div>
              );
            })}
          </Card>

          <div className="mt-3 text-xs text-ink-3">
            Requests awaiting your approval expire after {policy.approval_ttl_hours} hours.
          </div>
        </>
      )}
    </div>
  );
}
