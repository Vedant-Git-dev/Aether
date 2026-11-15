// Database use — the measured cost of the process's own queries. Read once
// when the page opens and only again on the explicit Refresh; this page is
// never polled, because the panel itself must not keep the database awake.
import { useCallback, useEffect, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Button, Card, Loading, SectionLabel } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import { api, hasToken } from "../lib/api.js";
import { humanize } from "../lib/describe.js";

function size(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1000) return `${Math.round(value)} B`;
  const units = ["KB", "MB", "GB"];
  let scaled = value / 1000;
  let unit = units[0];
  for (let i = 1; i < units.length && scaled >= 1000; i += 1) {
    scaled /= 1000;
    unit = units[i];
  }
  return `${scaled.toFixed(1)} ${unit}`;
}

function duration(seconds) {
  const value = Number(seconds) || 0;
  if (value < 60) return `${Math.round(value)} seconds`;
  if (value < 3600) return `${Math.round(value / 60)} minutes`;
  return `${(value / 3600).toFixed(1)} hours`;
}

// A code path reads as the part of Aether it belongs to. The precise
// function stays in the tooltip for debugging, never in the visible line.
const AREAS = [
  ["memory.vectors", "Memory search"],
  ["memory.retrieve", "Memory recall"],
  ["memory.indexes", "Memory index"],
  ["memory.lexical", "Memory index"],
  ["memory.storage", "Memory index"],
  ["memory.pipeline", "Memory recall"],
  ["memory.facts", "Remembered facts"],
  ["memory.commitments", "Promises and reminders"],
  ["memory.deliveries", "Reply delivery"],
  ["memory.inbound", "Incoming messages"],
  ["memory.entities", "People"],
  ["memory.events", "Event history"],
  ["memory.summaries", "Summaries"],
  ["proactive.path", "Reminders"],
  ["alerts.", "Alerts"],
  ["agent.loop", "Agent loop"],
  ["agent.traces", "Decision traces"],
  ["providers.", "App connections"],
  ["scheduler.", "Schedules"],
  ["authz.", "Approvals and decision record"],
  ["api.routes", "Panel reads"],
  ["connectors.", "Sources"],
  ["chat.ws", "Conversation transcript"],
  ["media.", "Media"],
  ["workspace.", "Workspace"],
];

function areaOf(site) {
  const name = String(site || "");
  for (const [prefix, label] of AREAS) {
    if (name.includes(`aether.${prefix}`) || name.startsWith(prefix)) return label;
  }
  return "Other work";
}

const TABLES = {
  memory_chunks: "Memory text and vectors",
  events: "Event history",
  audit_log: "Decision record",
  decision_traces: "Decision traces",
  provider_connections: "App sign-ins",
  provider_sync_state: "App checkpoints",
  pending_approvals: "Waiting for your approval",
  scheduled_actions: "Scheduled actions",
  deliveries: "Reply delivery record",
  commitments: "Promises",
  facts: "Remembered facts",
  chat_messages: "Conversation transcript",
  identities: "People",
  identity_handles: "People's addresses",
  bot_messages: "Conversation transcript",
};

function tableLabel(table) {
  return TABLES[table] || humanize(table) || "Startup checks";
}

function grouped(sites) {
  const groups = new Map();
  for (const site of sites || []) {
    const label = areaOf(site.site);
    const row = groups.get(label) || { label, calls: 0, bytes: 0, rows: 0, detail: [] };
    row.calls += Number(site.calls) || 0;
    row.bytes += Number(site.bytes) || 0;
    row.rows += Number(site.rows) || 0;
    row.detail.push(site);
    groups.set(label, row);
  }
  return [...groups.values()].sort((a, b) => b.bytes - a.bytes);
}

function HourBars({ hourly }) {
  const counts = hourly || [];
  const busiest = Math.max(1, ...counts);
  return (
    <div className="grid grid-cols-12 gap-x-2 gap-y-1.5 px-4 py-4 sm:grid-cols-[repeat(24,minmax(0,1fr))]">
      {counts.map((count, hour) => (
        <div key={hour} className="flex flex-col items-center gap-1" title={`${hour}:00 — ${count} queries`}>
          <div className="flex h-16 w-full items-end">
            <div
              className="w-full rounded-sm bg-ink/70"
              style={{ height: `${Math.max(2, Math.round((Number(count) / busiest) * 100))}%` }}
            />
          </div>
          <span className="text-[10px] text-ink-3">{hour}</span>
        </div>
      ))}
    </div>
  );
}

export default function DatabasePage() {
  const [stats, setStats] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!hasToken()) return;
    setLoading(true);
    try {
      setStats(await api("/api/db/stats"));
      setError("");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const totals = stats?.totals || {};
  const areas = grouped(stats?.sites);
  const tables = (stats?.tables || []).slice(0, 10);
  const connections = stats?.connections || {};
  const vectors = stats?.caches?.vectors || {};
  const vectorSummary = vectors.loaded
    ? `Memory search keeps ${Number(vectors.chunks || 0).toLocaleString()} chunks in this process (${Number(vectors.dims || 0)} dimensions, about ${size(vectors.bytes)}), so ranking reads no rows.${vectors.capped ? " The oldest chunks stay on the database and are out of the in-memory index." : ""}`
    : "Memory search loads its in-memory index when it first runs, and reads no rows to rank after that.";

  return (
    <div>
      <PageHead
        eyebrow="Operations"
        title="Database Use"
        sub="What Aether's own queries cost while this process has been running. Counts and estimated response sizes only — never message text."
      />
      {!hasToken() ? (
        <Card><NoToken /></Card>
      ) : error ? (
        <Card>
          <EmptyState title="Database use isn't available." sub={error} />
        </Card>
      ) : loading && !stats ? (
        <Card><Loading label="Reading the meters…" /></Card>
      ) : !stats ? (
        <Card>
          <EmptyState title="Nothing measured yet." sub="The meters fill as soon as Aether queries the database." />
        </Card>
      ) : (
        <div className="space-y-4">
          <Card>
            <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
              <div className="min-w-0">
                <div className="text-[13px] font-medium text-ink">
                  {Number(totals.calls || 0).toLocaleString()} queries since this process started
                </div>
                <div className="mt-1 text-[13px] text-ink-2">
                  About {size(totals.bytes)} of responses (estimated), {duration(stats.uptime_seconds)} of running,
                  {" "}{Number(totals.rows || 0).toLocaleString()} rows.
                </div>
              </div>
              <Button onClick={load} disabled={loading}>
                {loading ? "Reading…" : "Refresh"}
              </Button>
            </div>
            <SectionLabel>Connections</SectionLabel>
            <p className="px-4 py-3 text-[13px] leading-relaxed text-ink-2">
              {Number(connections.acquires || 0).toLocaleString()} leases from the shared pool,
              {" "}{Number(connections.peak || 0)} at once at most, {Number(connections.size || 0)} open now.
              The pool is reused by every part of Aether.
            </p>
            <SectionLabel>In memory</SectionLabel>
            <p className="px-4 py-3 text-[13px] leading-relaxed text-ink-2">{vectorSummary}</p>
          </Card>

          <Card>
            <SectionLabel>Busiest parts of Aether, by response size</SectionLabel>
            {areas.length === 0 ? (
              <EmptyState title="No queries yet." sub="Nothing has needed the database in this process." />
            ) : (
              <div className="divide-y divide-[var(--aether-hairline)]">
                {areas.slice(0, 10).map((area) => (
                  <div key={area.label} className="flex items-center justify-between gap-4 px-4 py-2.5">
                    <div className="min-w-0">
                      <div className="truncate text-[13px] text-ink">{area.label}</div>
                      <div className="text-[12px] text-ink-3">
                        {Number(area.calls).toLocaleString()} queries, {Number(area.rows).toLocaleString()} rows
                      </div>
                    </div>
                    <div className="text-right text-[13px] text-ink-2">{size(area.bytes)}</div>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card>
            <SectionLabel>Data read most, by response size</SectionLabel>
            {tables.length === 0 ? (
              <EmptyState title="No rows read yet." sub="Reads land here as Aether looks things up." />
            ) : (
              <div className="divide-y divide-[var(--aether-hairline)]">
                {tables.map((table) => (
                  <div key={table.table} className="flex items-center justify-between gap-4 px-4 py-2.5">
                    <div className="min-w-0">
                      <div className="truncate text-[13px] text-ink">{tableLabel(table.table)}</div>
                      <div className="text-[12px] text-ink-3">
                        {Number(table.calls).toLocaleString()} queries, {Number(table.rows).toLocaleString()} rows
                      </div>
                    </div>
                    <div className="text-right text-[13px] text-ink-2">{size(table.bytes)}</div>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card>
            <SectionLabel>When the queries happened</SectionLabel>
            <HourBars hourly={stats.hourly} />
            <p className="px-4 pb-3 text-[12px] text-ink-3">
              Hours are UTC. A quiet stretch here is when the database can go idle.
            </p>
          </Card>

          <p className="px-1 text-[12px] leading-relaxed text-ink-3">
            Response sizes are estimated from the records Aether received, not read from the database
            provider's meter. This page itself does not query the database.
          </p>
        </div>
      )}
    </div>
  );
}
