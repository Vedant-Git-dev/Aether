// The approvals page: what's parked for you right now, plus the policy aside
// and a "recently decided" strip carved from the already-polled audit feed.
import { useEffect, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Card, SectionLabel } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import ApprovalCard from "../components/ApprovalCard.jsx";
import Icon from "../components/Icon.jsx";
import { useData } from "../store.jsx";
import { hasToken } from "../lib/api.js";
import { fmtTime } from "../lib/format.js";
import { describeTool, describeOutcome, DECIDED_OUTCOME, SETTLED_OUTCOMES } from "../lib/describe.js";

function PolicyAside() {
  const { ensurePolicy } = useData();
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    ensurePolicy()
      .then((d) => alive && setData(d))
      .catch((e) => alive && setErr(e.message));
    return () => { alive = false; };
  }, [ensurePolicy]);

  if (err) return <Card className="p-4"><EmptyState title="Could not load the policy." sub={err} /></Card>;
  if (!data) return <Card className="p-4"><div className="py-4 text-sm text-ink-3">Loading policy…</div></Card>;

  return (
    <Card className="h-fit p-4">
      <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-ink">
        <Icon name="shield" className="h-4 w-4 text-ink-2" />
        Approval policy
      </h2>
      <p className="text-sm text-ink-2">
        Reading happens on its own. Anything that sends, changes or deletes something waits
        here for your approval.
      </p>
      <div className="mt-4 space-y-2 text-sm">
        <div className="flex justify-between">
          <span className="text-ink-3">Your own rules</span>
          <span className="font-medium text-ink">{data.rules.length}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-ink-3">Requests expire after</span>
          <span className="font-medium text-ink">{data.approval_ttl_hours} hours</span>
        </div>
      </div>
      <a href="#/policy" className="mt-4 inline-flex items-center gap-1 text-sm text-accent hover:underline">
        View full policy <Icon name="arrow" className="h-3.5 w-3.5" />
      </a>
    </Card>
  );
}

export default function AttentionPage() {
  const { approvals, audit, approvalUi, decide } = useData();

  const settled = (audit.entries || [])
    .filter((e) => DECIDED_OUTCOME.test(e.outcome || "") || SETTLED_OUTCOMES.has(e.outcome))
    .slice(0, 5);

  return (
    <div>
      <PageHead
        eyebrow="Your decision"
        title="Attention Required"
        sub="Actions Aether wants to take that are awaiting your approval first."
      />
      <div className="grid items-start gap-4 lg:grid-cols-[1fr_260px]">
        <div>
          {!hasToken() ? (
            <Card><NoToken /></Card>
          ) : approvals.length === 0 ? (
            <Card>
              <EmptyState
                title="Nothing requires your attention."
                sub="Aether is operating within its authorized boundaries."
              />
            </Card>
          ) : (
            <Card>
              {approvals.map((a) => (
                <ApprovalCard key={a.id} a={a} ui={approvalUi[a.id] || {}} onDecide={decide} />
              ))}
            </Card>
          )}

          {hasToken() && settled.length > 0 && (
            <>
              <SectionLabel className="mt-5 mb-2">Recently decided</SectionLabel>
              <Card>
                {settled.map((e) => (
                  <div key={e.seq} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-line px-4 py-2.5 text-sm last:border-b-0">
                    <span className="font-mono text-xs text-ink-3">{fmtTime(e.at)}</span>
                    <span className="text-ink-2">{describeTool(e.tool)}</span>
                    <span className="text-ink-3">{describeOutcome(e.outcome)}</span>
                  </div>
                ))}
              </Card>
            </>
          )}
        </div>

        {hasToken() && <PolicyAside />}
      </div>
    </div>
  );
}
