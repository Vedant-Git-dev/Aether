// Scheduled actions: persisted, survive restarts, gated by the same policy
// when they fire. Tab counts are plain "(n)" — the status is colored text.
// Below them, the background worker's finished work — each row carrying the
// same one-tap verdicts the chat surfaces offer, through the same decide
// path the chat buttons take.
import { useEffect, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Button, Card, SectionLabel } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import { useToast } from "../components/Toasts.jsx";
import { api, hasToken } from "../lib/api.js";
import { useBusyAction } from "../lib/busy.js";
import { fmtTime } from "../lib/format.js";
import { describeTool, describeSkill, describeFeedback, plainText } from "../lib/describe.js";
import { statusColor } from "../lib/status.js";

const TASK_STATUSES = ["all", "pending", "running", "done", "failed"];

const VERDICTS = {
  approve: { label: "Approve", kind: "approve" },
  revise: { label: "Revise", kind: "neutral" },
  discard: { label: "Discard", kind: "deny" },
};

export default function TasksPage() {
  const toast = useToast();
  const [tasks, setTasks] = useState(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("all");

  const [proactive, setProactive] = useState(null);
  const [proactiveError, setProactiveError] = useState("");
  const [busy, runBusy] = useBusyAction(); // task id -> the verdict in flight
  const [notes, setNotes] = useState({}); // task id -> the decide path's plain note

  useEffect(() => {
    if (!hasToken()) return;
    let alive = true;
    api("/api/tasks")
      .then((d) => alive && setTasks(d.tasks))
      .catch((e) => alive && setError(e.message));
    api("/api/proactive/tasks")
      .then((d) => alive && setProactive(d.tasks))
      .catch((e) => alive && setProactiveError(e.message));
    return () => { alive = false; };
  }, []);

  const count = (status) =>
    status === "all" ? (tasks || []).length : (tasks || []).filter((t) => t.status === status).length;

  const visible = (tasks || []).filter((t) => filter === "all" || t.status === filter);

  const decide = (id, verdict) =>
    runBusy(id, verdict, async () => {
      try {
        const d = await api(`/api/proactive/tasks/${id}/decide`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ verdict }),
        });
        // a null note means the carried-out action's own words are the
        // acknowledgment — the refetched row's plain verdict label says it
        setNotes((n) => ({ ...n, [id]: d.note || "" }));
      } catch (err) {
        setNotes((n) => ({ ...n, [id]: err.message }));
      } finally {
        api("/api/proactive/tasks")
          .then((d) => setProactive(d.tasks))
          .catch(() => {}); // the row's own state still tells the truth
      }
    });

  return (
    <div>
      <PageHead
        eyebrow="Automation"
        title="Tasks"
        sub="Scheduled actions — persisted, survive restarts, gated by the same authorization policy when they fire."
      />
      {!hasToken() ? (
        <Card><NoToken /></Card>
      ) : (
        <>
          <div className="aether-toolbar mb-4 inline-flex flex-wrap gap-1 p-1">
            {TASK_STATUSES.map((status) => (
              <button
                key={status}
                type="button"
                data-active={filter === status ? "1" : "0"}
                onClick={() => setFilter(status)}
                className="aether-tab capitalize"
              >
                {status}
                <span className="text-[11px] font-normal tabular-nums text-ink-3">
                  ({count(status)})
                </span>
              </button>
            ))}
          </div>
          <Card className="overflow-x-auto">
            {error ? (
              <EmptyState title="Could not load tasks." sub={error} />
            ) : tasks === null ? (
              <div className="px-4 py-10 text-center text-[13px] text-ink-3">Loading…</div>
            ) : tasks.length === 0 ? (
              <EmptyState
                title="Nothing scheduled."
                sub="Actions Aether schedules for later will appear here."
              />
            ) : visible.length === 0 ? (
              <EmptyState title="No tasks in this state." sub="Try a different tab above." />
            ) : (
              <table className="w-full min-w-[640px] text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-ink-3">
                    <th className="px-3 py-2 font-medium">Label</th>
                    <th className="px-3 py-2 font-medium">Run at</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 font-medium">Created</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((t) => (
                    <tr
                      key={t.id}
                      onClick={() => toast(`${t.label}: ${describeTool(t.payload && t.payload.tool).toLowerCase()}`)}
                      className="cursor-default border-b border-line last:border-b-0 hover:bg-subtle"
                    >
                      <td className="px-3 py-2 text-ink">{t.label}</td>
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-[11px] text-ink-2">{fmtTime(t.run_at)}</td>
                      <td className={`whitespace-nowrap px-3 py-2 font-medium capitalize ${statusColor(t.status)}`}>{t.status}</td>
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-[11px] text-ink-3">{fmtTime(t.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          <SectionLabel className="mt-7 mb-2">Background work</SectionLabel>
          <Card className="overflow-x-auto" data-testid="proactive-card">
            {proactiveError ? (
              <EmptyState title="Could not load background work." sub={proactiveError} />
            ) : proactive === null ? (
              <div className="px-4 py-10 text-center text-[13px] text-ink-3">Loading…</div>
            ) : proactive.length === 0 ? (
              <EmptyState
                title="No background work yet."
                sub="Results from Aether's background worker will appear here, waiting for your call."
              />
            ) : (
              <table className="w-full min-w-[840px] text-sm">
                <thead>
                  <tr className="border-b border-line text-left text-xs text-ink-3">
                    <th className="px-3 py-2 font-medium">Task</th>
                    <th className="px-3 py-2 font-medium">Kind</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 font-medium">Result</th>
                    <th className="px-3 py-2 font-medium">Your call</th>
                    <th className="px-3 py-2 font-medium">Created</th>
                  </tr>
                </thead>
                <tbody>
                  {proactive.map((t) => (
                    <tr
                      key={t.id}
                      data-testid="proactive-row"
                      className="border-b border-line last:border-b-0"
                    >
                      <td className="px-3 py-2 text-ink">{t.label}</td>
                      <td className="px-3 py-2 text-ink-2">{describeSkill(t.skill)}</td>
                      <td className={`whitespace-nowrap px-3 py-2 font-medium capitalize ${statusColor(t.status)}`}>{t.status}</td>
                      <td className="max-w-sm px-3 py-2 text-xs text-ink-2">{plainText(t.result)}</td>
                      <td className="px-3 py-2">
                        {t.feedback ? (
                          <div>
                            <div className={`font-medium ${statusColor(t.feedback)}`}>{describeFeedback(t.feedback)}</div>
                            {t.feedback_note && (
                              <div className="text-xs text-ink-3">{plainText(t.feedback_note)}</div>
                            )}
                          </div>
                        ) : t.options.length > 0 ? (
                          <div className="flex flex-wrap items-center gap-1.5">
                            {t.options.map((v) => (
                              <Button
                                key={v}
                                kind={VERDICTS[v].kind}
                                data-testid={`proactive-${v}-${t.id}`}
                                disabled={Boolean(busy[t.id])}
                                onClick={() => decide(t.id, v)}
                              >
                                {VERDICTS[v].label}
                              </Button>
                            ))}
                          </div>
                        ) : null}
                        {notes[t.id] && (
                          <div data-testid={`proactive-note-${t.id}`} className="text-xs text-ink-3">
                            {notes[t.id]}
                          </div>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 font-mono text-[11px] text-ink-3">{fmtTime(t.created_at)}</td>
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
