// Memory & Context: what Aether remembers, in plain language, and the one
// place it can be made to forget. The backend authors every sentence this
// page shows (the `plain` field); nothing internal — an id, an event kind, a
// raw timestamp — is rendered. The context graph below is unchanged.
import { useEffect, useMemo, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Card, Button, Field, Loading } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import PersonCard from "../components/PersonCard.jsx";
import { useData } from "../store.jsx";
import { api, hasToken } from "../lib/api.js";
import { fmtTime } from "../lib/format.js";
import { describeClaimKind, describeMemoryItem } from "../lib/describe.js";

function ForgetControl({ id, busy, onForget }) {
  const [confirming, setConfirming] = useState(false);
  if (!busy && !confirming) {
    return (
      <Button kind="neutral" className="px-2.5 py-1 text-[12px]" onClick={() => setConfirming(true)}>
        Forget
      </Button>
    );
  }
  return (
    <span className="inline-flex items-center gap-2">
      <Button
        kind="deny"
        className="px-2.5 py-1 text-[12px]"
        disabled={busy}
        onClick={() => onForget(id)}
      >
        {busy ? "Forgetting..." : "Forget it"}
      </Button>
      {!busy && (
        <button
          type="button"
          className="text-[12px] text-ink-3 hover:text-ink"
          onClick={() => setConfirming(false)}
        >
          Cancel
        </button>
      )}
    </span>
  );
}

function StatusLine({ overview }) {
  if (!overview) return null;
  const semantic = overview.semantic_search || {};
  const parts = [
    [`${overview.resident_events}`, "remembered items in view"],
    [`${overview.indexed_chunks}`, "searchable pieces"],
    [`${overview.active_facts}`, "things it knows"],
    [`${overview.open_commitments}`, "open promises"],
    [`${overview.active_watches}`, "active watches"],
  ];
  return (
    <div className="mb-4 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-ink-3">
      {parts.map(([count, word], i) => (
        <span key={word} className="inline-flex items-center gap-2">
          {i > 0 && <span aria-hidden="true">·</span>}
          <span className="font-medium text-ink-2 tabular-nums">{count}</span>
          <span>{word}</span>
        </span>
      ))}
      <span aria-hidden="true">·</span>
      <span>{semantic.on ? "search by meaning is on" : semantic.note}</span>
    </div>
  );
}

export default function MemoryPage() {
  const { people, updatePeople, ensurePeople } = useData();
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [overview, setOverview] = useState(null);
  const [items, setItems] = useState([]);
  const [claims, setClaims] = useState([]);
  const [busy, setBusy] = useState("");

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

  useEffect(() => {
    if (!hasToken()) return;
    let alive = true;
    Promise.all([
      api("/api/memory/overview"),
      api("/api/memory/items"),
      api("/api/memory/claims"),
    ])
      .then(([o, i, c]) => {
        if (!alive) return;
        setOverview(o);
        setItems(i.items || []);
        setClaims(c.claims || []);
      })
      .catch((e) => alive && setError(e.message));
    return () => { alive = false; };
  }, [loaded]);

  useEffect(() => {
    if (!hasToken() || !loaded) return;
    let alive = true;
    const timer = setTimeout(() => {
      api(`/api/memory/items?q=${encodeURIComponent(search)}`)
        .then((d) => alive && setItems(d.items || []))
        .catch((e) => alive && setError(e.message));
    }, 250);
    return () => { alive = false; clearTimeout(timer); };
  }, [search, loaded]);

  const handleCount = useMemo(
    () => people.reduce((n, p) => n + p.handles.length, 0),
    [people],
  );

  async function forget(kind, id) {
    setBusy(`${kind}:${id}`);
    setError("");
    try {
      const result = await api("/api/memory/forget", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ kind, id }),
      });
      if (result.overview) setOverview(result.overview);
      if (kind === "event") setItems((rows) => rows.filter((row) => row.id !== id));
      else setClaims((rows) => rows.filter((row) => !(row.id === id && row.kind === kind)));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

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
        sub="What Aether remembers, and the people it has resolved across platforms. Forgetting something here removes it for good."
      />
      {!hasToken() ? (
        <Card><NoToken /></Card>
      ) : !loaded ? (
        <Card><Loading /></Card>
      ) : (
        <>
          <StatusLine overview={overview} />
          {error && (
            <Card className="mb-4 px-4 py-3">
              <p className="text-[13px] text-danger">{error}</p>
            </Card>
          )}

          <Card className="mb-4 p-5">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <h2 className="text-[13px] font-medium text-ink">Remembered items</h2>
              <Field
                type="search"
                placeholder="Search memory"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="w-full max-w-xs"
                data-testid="memory-search"
              />
            </div>
            {items.length === 0 ? (
              <EmptyState
                title={search ? "Nothing matches." : "Nothing remembered yet."}
                sub={
                  search
                    ? "Try another word, or clear the search."
                    : "Items Aether sees will appear here as it remembers them."
                }
              />
            ) : (
              <ul className="divide-y divide-[var(--aether-hairline)]">
                {items.map((item) => (
                  <li
                    key={item.id}
                    data-testid={`memory-item-${item.id}`}
                    className="flex items-center justify-between gap-3 py-2.5"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-[13px] text-ink">{describeMemoryItem(item)}</p>
                      <p className="text-[11.5px] text-ink-3">{fmtTime(item.at)}</p>
                    </div>
                    <ForgetControl
                      id={item.id}
                      busy={busy === `event:${item.id}`}
                      onForget={(id) => forget("event", id)}
                    />
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {claims.length > 0 && (
            <Card className="mb-4 p-5">
              <h2 className="mb-3 text-[13px] font-medium text-ink">Claims and promises</h2>
              <ul className="divide-y divide-[var(--aether-hairline)]">
                {claims.map((claim) => (
                  <li
                    key={`${claim.kind}:${claim.id}`}
                    data-testid={`memory-claim-${claim.kind}-${claim.id}`}
                    className="flex items-center justify-between gap-3 py-2.5"
                  >
                    <div className="min-w-0">
                      <p className="text-[11px] uppercase tracking-wide text-ink-3">
                        {describeClaimKind(claim.kind)}
                      </p>
                      <p className="truncate text-[13px] text-ink">{claim.plain}</p>
                    </div>
                    <ForgetControl
                      id={claim.id}
                      busy={busy === `${claim.kind}:${claim.id}`}
                      onForget={(id) => forget(claim.kind, id)}
                    />
                  </li>
                ))}
              </ul>
            </Card>
          )}

          <div className="mb-4 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-ink-3">
            <span className="font-medium text-ink-2 tabular-nums">{people.length}</span> recognized
            people
            <span aria-hidden="true">·</span>
            <span className="font-medium text-ink-2 tabular-nums">{handleCount}</span> linked
            identities
            <span aria-hidden="true">·</span>
            <span>encrypted at rest</span>
          </div>
          {people.length === 0 ? (
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
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {visible.map((p) => <PersonCard key={p.id} person={p} />)}
            </div>
          )}
        </>
      )}
    </div>
  );
}
