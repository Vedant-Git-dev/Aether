// Scheduled actions: persisted, survive restarts, gated by the same policy
// when they fire. Tab counts are plain "(n)" — the status is colored text.
import { useEffect, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Card } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import { useToast } from "../components/Toasts.jsx";
import { api, hasToken } from "../lib/api.js";
import { fmtTime } from "../lib/format.js";
import { describeTool } from "../lib/describe.js";
import { statusColor } from "../lib/status.js";

const TASK_STATUSES = ["all", "pending", "running", "done", "failed"];

export default function TasksPage() {
  const toast = useToast();
  const [tasks, setTasks] = useState(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("all");

  useEffect(() => {
    if (!hasToken()) return;
    let alive = true;
    api("/api/tasks")
      .then((d) => alive && setTasks(d.tasks))
      .catch((e) => alive && setError(e.message));
    return () => { alive = false; };
  }, []);

  const count = (status) =>
    status === "all" ? (tasks || []).length : (tasks || []).filter((t) => t.status === status).length;

  const visible = (tasks || []).filter((t) => filter === "all" || t.status === filter);

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
          <div className="mb-3 flex flex-wrap gap-1.5">
            {TASK_STATUSES.map((status) => (
              <button
                key={status}
                type="button"
                onClick={() => setFilter(status)}
                className={`rounded-md border px-3 py-1.5 text-sm capitalize
                  ${filter === status
                    ? "border-accent bg-subtle font-medium text-ink"
                    : "border-line-strong bg-card text-ink-2 hover:bg-subtle"}`}
              >
                {status} <span className="text-ink-3">({count(status)})</span>
              </button>
            ))}
          </div>
          <Card className="overflow-x-auto">
            {error ? (
              <EmptyState title="Could not load tasks." sub={error} />
            ) : tasks === null ? (
              <div className="px-4 py-6 text-sm text-ink-3">Loading…</div>
            ) : tasks.length === 0 ? (
              <EmptyState
                title="Nothing scheduled."
                sub="Actions Aether schedules for later will appear here."
              />
            ) : visible.length === 0 ? (
              <EmptyState title="No tasks in this state." sub="Try a different tab above." />
            ) : (
              <table className="w-full text-sm">
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
                      <td className="px-3 py-2 font-mono text-xs text-ink-2">{fmtTime(t.run_at)}</td>
                      <td className={`px-3 py-2 font-medium capitalize ${statusColor(t.status)}`}>{t.status}</td>
                      <td className="px-3 py-2 font-mono text-xs text-ink-3">{fmtTime(t.created_at)}</td>
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
