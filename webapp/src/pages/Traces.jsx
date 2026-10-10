// What triggered each action, how the gate ruled on every call, what came
// back. Rows expand lazily; open state lives in the store so it survives a
// detour to another page.
import { useEffect } from "react";
import PageHead from "../components/PageHead.jsx";
import { Card } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import TraceRow from "../components/TraceRow.jsx";
import { useData } from "../store.jsx";
import { hasToken } from "../lib/api.js";

export default function TracesPage() {
  const { traces, tracesError, tracesOpen, traceDetails, toggleTrace, ensurePeople } = useData();

  useEffect(() => { ensurePeople(); }, [ensurePeople]);

  return (
    <div>
      <PageHead
        eyebrow="Reasoning"
        title="Decision Traces"
        sub="What triggered each action, how the authorization gate ruled on every call, and what came back."
      />
      {!hasToken() ? (
        <Card><NoToken /></Card>
      ) : tracesError ? (
        <Card>
          <EmptyState title="Decision traces aren't available." sub={tracesError} />
        </Card>
      ) : traces.length === 0 ? (
        <Card>
          <EmptyState
            title="No traces yet."
            sub="Aether records the reasoning behind every action — conversations, routines, scheduled actions and approved calls land here."
          />
        </Card>
      ) : (
        <Card>
          {traces.map((t) => (
            <TraceRow
              key={t.id}
              t={t}
              open={tracesOpen.has(t.id)}
              detail={traceDetails[t.id]}
              onToggle={toggleTrace}
            />
          ))}
        </Card>
      )}
    </div>
  );
}
