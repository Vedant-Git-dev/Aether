// Alerts and reminders: what Aether watches for you and when it speaks.
import { useEffect, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Button, Card, SectionLabel } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import { useToast } from "../components/Toasts.jsx";
import { api, hasToken } from "../lib/api.js";
import { useBusyAction } from "../lib/busy.js";
import { fmtTime } from "../lib/format.js";
import { statusColor } from "../lib/status.js";
import { describeAlertStyle } from "../lib/describe.js";

function Rows({ rows, busy, onToggle, onDelete }) {
  return (
    <table className="w-full min-w-[760px] text-sm">
      <thead>
        <tr className="border-b border-line text-left text-xs text-ink-3">
          <th className="px-3 py-2 font-medium">Name</th>
          <th className="px-3 py-2 font-medium">When it speaks</th>
          <th className="px-3 py-2 font-medium">Style</th>
          <th className="px-3 py-2 font-medium">State</th>
          <th className="px-3 py-2 font-medium">Last spoken</th>
          <th className="px-3 py-2 font-medium">Your call</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id} data-testid="alert-row" className="border-b border-line last:border-b-0">
            <td className="px-3 py-2 text-ink">{row.title || "Untitled"}</td>
            <td className="max-w-sm px-3 py-2 text-ink-2">
              {row.kind === "reminder" ? row.when : row.condition}
            </td>
            <td className="px-3 py-2 text-ink-2">{describeAlertStyle(row.style)}</td>
            <td className={`whitespace-nowrap px-3 py-2 font-medium capitalize ${statusColor(row.status)}`}>
              {row.status}
            </td>
            <td className="whitespace-nowrap px-3 py-2 font-mono text-[11px] tabular-nums text-ink-3">
              {row.last_notified_at ? fmtTime(row.last_notified_at) : "—"}
            </td>
            <td className="px-3 py-2">
              <div className="flex flex-wrap items-center gap-1.5">
                <Button
                  kind="neutral"
                  disabled={Boolean(busy[row.id])}
                  onClick={() => onToggle(row)}
                >
                  {row.status === "active" ? "Pause" : "Resume"}
                </Button>
                <Button kind="deny" disabled={Boolean(busy[row.id])} onClick={() => onDelete(row)}>
                  Remove
                </Button>
              </div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function AlertsPage() {
  const toast = useToast();
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");
  const [busy, runBusy] = useBusyAction();

  const load = () =>
    api("/api/alerts")
      .then((d) => {
        setRows(d.alerts);
        setError("");
      })
      .catch((e) => setError(e.message));

  useEffect(() => {
    if (!hasToken()) return;
    load();
  }, []);

  const toggle = (row) =>
    runBusy(row.id, "toggle", async () => {
      try {
        const d = await api(`/api/alerts/${row.id}`, {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ enabled: row.status !== "active" }),
        });
        toast(d.message);
      } catch (err) {
        toast(err.message, "err");
      } finally {
        load();
      }
    });

  const remove = (row) =>
    runBusy(row.id, "delete", async () => {
      try {
        const d = await api(`/api/alerts/${row.id}`, { method: "DELETE" });
        toast(d.message);
      } catch (err) {
        toast(err.message, "err");
      } finally {
        load();
      }
    });

  const alerts = (rows || []).filter((row) => row.kind !== "reminder");
  const reminders = (rows || []).filter((row) => row.kind === "reminder");

  return (
    <div>
      <PageHead
        eyebrow="Standing instructions"
        title="Alerts"
        sub="What Aether tells you about when it happens, and what it reminds you of at a time."
      />
      {!hasToken() ? (
        <Card><NoToken /></Card>
      ) : error ? (
        <Card><EmptyState title="Could not load alerts." sub={error} /></Card>
      ) : rows === null ? (
        <Card><div className="px-4 py-10 text-center text-[13px] text-ink-3">Loading…</div></Card>
      ) : rows.length === 0 ? (
        <Card>
          <EmptyState
            title="Nothing set."
            sub="Ask Aether in chat to tell you when something happens, or to remind you at a time."
          />
        </Card>
      ) : (
        <>
          <SectionLabel className="mb-2">Alerts</SectionLabel>
          <Card className="overflow-x-auto">
            {alerts.length === 0 ? (
              <EmptyState title="No alerts." sub="Aether isn't keeping an eye on anything yet." />
            ) : (
              <Rows rows={alerts} busy={busy} onToggle={toggle} onDelete={remove} />
            )}
          </Card>

          <SectionLabel className="mt-8 mb-2">Reminders</SectionLabel>
          <Card className="overflow-x-auto">
            {reminders.length === 0 ? (
              <EmptyState title="No reminders." sub="A time you asked Aether to remind you of will appear here." />
            ) : (
              <Rows rows={reminders} busy={busy} onToggle={toggle} onDelete={remove} />
            )}
          </Card>
        </>
      )}
    </div>
  );
}
