// The tamper-evident log: every policy decision in a hash chain. The chain
// verdict is plain colored text beside the head hash — no banner fills.
import { useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Card, Field, Select } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import Icon from "../components/Icon.jsx";
import { useToast } from "../components/Toasts.jsx";
import { useData } from "../store.jsx";
import { hasToken } from "../lib/api.js";
import { fmtTime, shortHash } from "../lib/format.js";
import { describeTool, describeOutcome, describeRule, humanize, ACTOR_LABELS, AUDIT_DECISIONS } from "../lib/describe.js";
import { statusColor } from "../lib/status.js";

export default function AuditPage() {
  const { audit } = useData();
  const toast = useToast();
  const [decisionFilter, setDecisionFilter] = useState("");
  const [search, setSearch] = useState("");

  const chain = audit.chain;
  const entries = audit.entries || [];

  const rows = entries.filter((e) => {
    if (decisionFilter && e.decision !== decisionFilter) return false;
    if (search && !`${describeTool(e.tool)} ${describeOutcome(e.outcome)} ${describeRule(e.rules_matched)}`
      .toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  return (
    <div>
      <PageHead
        eyebrow="Verifiability"
        title="Audit Log"
        sub="Every decision Aether's policy made, in a tamper-evident hash chain."
      />
      {!hasToken() ? (
        <Card><NoToken /></Card>
      ) : (
        <>
          {chain && (
            <Card className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 p-4">
              <span className="flex items-center gap-2">
                <Icon name="shield" className={`h-4.5 w-4.5 ${chain.ok ? "text-success" : "text-danger"}`} />
                <span className={`text-sm font-medium ${chain.ok ? "text-success" : "text-danger"}`}>
                  {chain.ok ? "Chain integrity verified" : "Chain integrity broken"}
                </span>
              </span>
              <span className="text-xs text-ink-2">
                {chain.ok
                  ? `All ${chain.entries} record${chain.entries === 1 ? "" : "s"} are valid.`
                  : `Broken at #${chain.first_bad_seq}: ${chain.problem || "mismatch"}`}
              </span>
              <span className="ml-auto flex items-center gap-1.5 text-xs text-ink-3">
                head hash <code className="font-mono text-ink-2">{shortHash(chain.head_hash)}</code>
              </span>
            </Card>
          )}

          <div className="mb-3 flex flex-wrap gap-2">
            <Select value={decisionFilter} onChange={(e) => setDecisionFilter(e.target.value)}>
              <option value="">All decisions</option>
              <option value="allow">Allowed</option>
              <option value="approve">Held for approval</option>
              <option value="deny">Blocked</option>
              <option value="filtered">Filtered out</option>
              <option value="info">Noted</option>
            </Select>
            <Field
              type="search"
              placeholder="Search actions"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>

          <Card className="overflow-x-auto">
            {entries.length === 0 ? (
              <EmptyState
                title="No entries yet."
                sub="Actions will appear here as Aether's policy evaluates them."
              />
            ) : rows.length === 0 ? (
              <EmptyState title="No entries match these filters." />
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-ink-3">
                    <th className="px-3 py-2 font-medium">Seq</th>
                    <th className="px-3 py-2 font-medium">Time</th>
                    <th className="px-3 py-2 font-medium">Who</th>
                    <th className="px-3 py-2 font-medium">Action</th>
                    <th className="px-3 py-2 font-medium">Decision</th>
                    <th className="px-3 py-2 font-medium">Why</th>
                    <th className="px-3 py-2 font-medium">Result</th>
                    <th className="px-3 py-2 font-medium">Chain</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((e) => (
                    <tr
                      key={e.seq}
                      onClick={() => toast(`#${e.seq} · ${describeTool(e.tool)}`)}
                      className="cursor-default border-b border-line last:border-b-0 hover:bg-subtle"
                    >
                      <td className="px-3 py-2 font-mono text-xs text-ink-3">#{e.seq}</td>
                      <td className="px-3 py-2 font-mono text-xs text-ink-3">{fmtTime(e.at)}</td>
                      <td className="px-3 py-2 text-ink-2">{ACTOR_LABELS[e.actor] || humanize(e.actor)}</td>
                      <td className="px-3 py-2 text-ink">{describeTool(e.tool)}</td>
                      <td className={`px-3 py-2 font-medium ${statusColor(e.decision)}`}>
                        {AUDIT_DECISIONS[e.decision] || humanize(e.decision)}
                      </td>
                      <td className="px-3 py-2 text-xs text-ink-3">{describeRule(e.rules_matched)}</td>
                      <td className="px-3 py-2 text-xs text-ink-2">{describeOutcome(e.outcome)}</td>
                      <td className="px-3 py-2 font-mono text-xs text-ink-3">{shortHash(e.hash)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
