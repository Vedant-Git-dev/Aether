// One context provider owns all server state and all polling — the faithful
// React mirror of the old panel's module-level `state` + global pollers.
// Pages consume via useData(); cross-page caching (people, traces, policy)
// survives navigation exactly as before.
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, hasToken } from "./lib/api.js";
import { setPeople as bridgePeople } from "./lib/describe.js";

const DataContext = createContext(null);

export function useData() {
  return useContext(DataContext);
}

export function DataProvider({ children }) {
  const [approvals, setApprovals] = useState([]);
  const [events, setEvents] = useState([]);
  const [audit, setAudit] = useState({ entries: [], chain: null });
  const [traces, setTraces] = useState([]);
  const [tracesError, setTracesError] = useState("");
  const [policy, setPolicy] = useState(null);
  const [people, setPeople] = useState([]);
  const [approvalUi, setApprovalUi] = useState({});
  const [tracesOpen, setTracesOpen] = useState(() => new Set());
  const [traceDetails, setTraceDetails] = useState({});

  const peopleLoaded = useRef(false);
  const decideTimers = useRef(new Set());
  const traceDetailsRef = useRef({});

  const refreshApprovals = useCallback(async () => {
    if (!hasToken()) return;
    try {
      const data = await api("/api/approvals");
      setApprovals(data.pending);
    } catch { return; /* transient network hiccup — keep the last known list */ }
  }, []);

  const refreshEvents = useCallback(async () => {
    if (!hasToken()) return;
    try {
      const data = await api("/api/events?limit=100");
      setEvents(data.events);
    } catch { return; }
  }, []);

  const refreshAudit = useCallback(async () => {
    if (!hasToken()) return;
    try {
      const data = await api("/api/audit?limit=200");
      setAudit(data);
    } catch { return; }
  }, []);

  const refreshTraces = useCallback(async () => {
    if (!hasToken()) return;
    try {
      const data = await api("/api/traces?limit=50");
      setTraces(data.traces);
      setTracesError("");
    } catch (err) {
      setTracesError(err.message); // e.g. not wired — the page says so honestly
    }
  }, []);

  // the four pollers run globally from boot, exactly like the old panel
  useEffect(() => {
    refreshApprovals(); refreshEvents(); refreshAudit(); refreshTraces();
    const handles = [
      setInterval(refreshApprovals, 5000),
      setInterval(refreshEvents, 10000),
      setInterval(refreshAudit, 10000),
      setInterval(refreshTraces, 10000),
    ];
    return () => handles.forEach(clearInterval);
  }, [refreshApprovals, refreshEvents, refreshAudit, refreshTraces]);

  useEffect(() => () => {
    decideTimers.current.forEach(clearTimeout);
    decideTimers.current.clear();
  }, []);

  const updatePeople = useCallback((list) => {
    setPeople(list || []);
    bridgePeople(list || []);
  }, []);

  const ensurePeople = useCallback(async () => {
    if (peopleLoaded.current || !hasToken()) return;
    try {
      const data = await api("/api/memory");
      updatePeople(data.people);
      peopleLoaded.current = true;
    } catch { /* names fall back to handles */ }
  }, [updatePeople]);

  const decide = useCallback(async (id, decision) => {
    setApprovalUi((ui) => ({ ...ui, [id]: { pending: true } }));
    try {
      const res = await api(`/api/approvals/${id}/decide`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      setApprovalUi((ui) => ({
        ...ui,
        [id]: {
          pending: false,
          result: res.ok
            ? { ok: true, msg: decision === "approve" ? "Approved — Aether will proceed." : "Denied — Aether will not run this." }
            : { ok: false, msg: res.reason === "already decided or expired" ? "This request was already handled or has expired." : (res.reason || "Your decision wasn't applied.") },
        },
      }));
    } catch (err) {
      setApprovalUi((ui) => ({ ...ui, [id]: { pending: false, result: { ok: false, msg: err.message } } }));
    }
    // give the user a beat to actually see the confirmation before the next
    // refetch removes the card (an immediate refetch made it disappear before
    // a single frame ever painted it — the e2e approve test depends on this)
    const t = setTimeout(() => refreshApprovals(), 1200);
    decideTimers.current.add(t);
    setTimeout(() => decideTimers.current.delete(t), 2000);
  }, [refreshApprovals]);

  // open/close a trace; the payload-bearing detail lazy-fetches once and then
  // stays cached — open state and details both survive page switches
  const toggleTrace = useCallback(async (id) => {
    setTracesOpen((open) => {
      const next = new Set(open);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    if (traceDetailsRef.current[id] || !hasToken()) return;
    let detail;
    try {
      detail = await api(`/api/traces/${id}`);
    } catch (err) {
      detail = { id, kind: "error", label: "", at: "", payload: { error: err.message } };
    }
    traceDetailsRef.current[id] = detail;
    setTraceDetails((details) => ({ ...details, [id]: detail }));
  }, []);

  const ensurePolicy = useCallback(async () => {
    if (policy && policy.rules) return policy;
    const data = await api("/api/policy");
    setPolicy(data);
    return data;
  }, [policy]);

  const value = {
    approvals, events, audit, traces, tracesError, policy, people,
    approvalUi, tracesOpen, traceDetails,
    refreshApprovals, updatePeople, ensurePeople, decide, toggleTrace, ensurePolicy,
  };

  return <DataContext.Provider value={value}>{children}</DataContext.Provider>;
}
