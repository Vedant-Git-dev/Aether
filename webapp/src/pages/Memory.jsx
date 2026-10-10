// The context graph: cross-platform identities Aether has resolved, and the
// most recent relationship note for each. One muted summary line — no chips.
import { useEffect, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Card, Field } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import PersonCard from "../components/PersonCard.jsx";
import { useData } from "../store.jsx";
import { api, hasToken } from "../lib/api.js";

export default function MemoryPage() {
  const { people, updatePeople, ensurePeople } = useData();
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!hasToken() || loaded) return;
    let alive = true;
    // this page always shows the current graph — it refetches on every visit
    api("/api/memory")
      .then((d) => { if (alive) { updatePeople(d.people); setLoaded(true); } })
      .catch((e) => alive && setError(e.message));
    // but describe() only needs people once per session
    ensurePeople();
    return () => { alive = false; };
  }, [loaded, updatePeople, ensurePeople]);

  const handleCount = people.reduce((n, p) => n + p.handles.length, 0);

  const visible = people.filter((p) => {
    if (!search) return true;
    const note = p.latest_note ? JSON.stringify(p.latest_note.payload) : "";
    return `${p.display_name} ${note}`.toLowerCase().includes(search.toLowerCase());
  });

  return (
    <div>
      <PageHead
        eyebrow="Context graph"
        title="Memory & Context"
        sub="Cross-platform identities Aether has resolved, and the most recent relationship note for each."
      />
      {!hasToken() ? (
        <Card><NoToken /></Card>
      ) : (
        <>
          <div className="mb-3 text-xs text-ink-3">
            {people.length} recognized people · {handleCount} linked identities · encrypted at rest
          </div>
          <div className="mb-3">
            <Field
              type="search"
              placeholder="Search people"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          {error ? (
            <Card><EmptyState title="Could not load memory & context." sub={error} /></Card>
          ) : !loaded ? (
            <Card><div className="px-4 py-6 text-sm text-ink-3">Loading…</div></Card>
          ) : people.length === 0 ? (
            <Card>
              <EmptyState
                title="No one recognized yet."
                sub="Aether links identities across platforms as it observes them — they'll appear here."
              />
            </Card>
          ) : visible.length === 0 ? (
            <Card>
              <EmptyState title="No matching people." sub="Try another name or detail from your context graph." />
            </Card>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2">
              {visible.map((p) => <PersonCard key={p.id} person={p} />)}
            </div>
          )}
        </>
      )}
    </div>
  );
}
