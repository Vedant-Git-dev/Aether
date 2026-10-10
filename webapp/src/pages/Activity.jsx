// The live feed: everything Aether observed or did, newest first, filterable
// by app, free text and importance. The app is a plain muted prefix — no chip.
import { useEffect, useMemo, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Card, Field, Select } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import { useData } from "../store.jsx";
import { hasToken } from "../lib/api.js";
import { fmtTime } from "../lib/format.js";
import { appLabel, describeEvent } from "../lib/describe.js";

export default function ActivityPage() {
  const { events, ensurePeople } = useData();
  const [source, setSource] = useState("");
  const [search, setSearch] = useState("");
  const [salientOnly, setSalientOnly] = useState(false);

  // one fetch per session — names stop falling back to handles once it lands
  useEffect(() => { ensurePeople(); }, [ensurePeople]);

  const sources = useMemo(
    () => Array.from(new Set(events.map((e) => e.source))).sort(),
    [events],
  );

  const filtered = events.filter((e) => {
    if (source && e.source !== source) return false;
    if (salientOnly && !e.memorable) return false;
    if (search) {
      const { title, detail } = describeEvent(e);
      if (!`${title} ${detail} ${appLabel(e.source)}`.toLowerCase().includes(search.toLowerCase())) return false;
    }
    return true;
  });

  return (
    <div>
      <PageHead
        eyebrow="What's happening"
        title="Live Activity"
        sub="What Aether has seen and done across your apps, newest first."
      />
      {!hasToken() ? (
        <Card><NoToken /></Card>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Select
              data-testid="filter-source"
              value={source}
              onChange={(e) => setSource(e.target.value)}
            >
              <option value="">All apps</option>
              {sources.map((s) => <option key={s} value={s}>{appLabel(s)}</option>)}
            </Select>
            <Field
              type="search"
              placeholder="Search activity"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <label className="flex items-center gap-1.5 text-xs text-ink-2">
              <input
                type="checkbox"
                checked={salientOnly}
                onChange={(e) => setSalientOnly(e.target.checked)}
                className="accent-[var(--color-accent)]"
              />
              Important only
            </label>
          </div>

          <Card>
            {events.length === 0 ? (
              <EmptyState
                title="No activity yet."
                sub="Events will appear here as Aether observes your connected apps."
              />
            ) : filtered.length === 0 ? (
              <EmptyState title="No events match these filters." sub="Try clearing a filter above." />
            ) : (
              filtered.map((e) => {
                const { title, detail } = describeEvent(e);
                return (
                  <div
                    key={e.id}
                    data-testid="event-row"
                    className="flex gap-3 border-b border-line px-4 py-2.5 last:border-b-0"
                  >
                    <span className="shrink-0 font-mono text-xs leading-5 text-ink-3">{fmtTime(e.at)}</span>
                    <div className="min-w-0">
                      <div className={`text-sm text-ink ${e.memorable ? "font-medium" : ""}`}>
                        <span className="text-ink-3">{appLabel(e.source)} · </span>
                        {title}
                      </div>
                      {detail ? <div className="mt-0.5 text-xs text-ink-3">{detail}</div> : null}
                    </div>
                  </div>
                );
              })
            )}
          </Card>
        </>
      )}
    </div>
  );
}
