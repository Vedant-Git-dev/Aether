"use strict";
/* Aether control center — vanilla JS SPA, no build step.
   Talks to the real REST + WebSocket surface in aether.api / aether.chat.
   Nothing here fabricates data: every panel renders what the backend
   actually returned, or an honest empty/error state. */

// --------------------------------------------------------------- dom utils

const $ = (id) => document.getElementById(id);
const qs = (sel, root) => (root || document).querySelector(sel);
const qsa = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === undefined || kid === null || kid === false) continue;
    node.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
  }
  return node;
}

const ICONS = {
  activity: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 12h4l2.5-7 5 14 2.5-7H21"/></svg>',
  attention: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 4 2 20h20L12 4z"/><path d="M12 10v4M12 17h.01"/></svg>',
  audit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="4" y="3" width="16" height="18" rx="1.5"/><path d="M8 8h8M8 12h8M8 16h5"/></svg>',
  traces: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="5" cy="5" r="2"/><circle cx="5" cy="12" r="2"/><circle cx="5" cy="19" r="2"/><path d="M9.5 5H19M9.5 12h6.5M9.5 19H19"/></svg>',
  policy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3z"/></svg>',
  apps: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/></svg>',
  tasks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m4 12 4 4 12-12"/><path d="M4 6h9M4 18h9" stroke-opacity=".5"/></svg>',
  memory: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="9" cy="8" r="3.2"/><path d="M4 20c0-3 2.2-5 5-5s5 2 5 5"/><circle cx="17" cy="7" r="2.2" stroke-opacity=".6"/><path d="M14.5 12.5c1.8.4 3 1.8 3.2 3.7" stroke-opacity=".6"/></svg>',
  settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="3"/><path d="M19.4 13.5a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.04 1.56V19.6a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.04-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1.04H2.4a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.56-1.04 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.87.34H8.5a1.7 1.7 0 0 0 1.04-1.56V2.4a2 2 0 1 1 4 0v.09c0 .68.4 1.29 1.04 1.56.62.26 1.34.14 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87c.26.62.87 1.04 1.56 1.04h.09a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.56 1.04z"/></svg>',
  collapse: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 6h16M4 12h10M4 18h16"/></svg>',
  menu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 6h16M4 12h16M4 18h16"/></svg>',
  bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M6 9a6 6 0 1 1 12 0c0 4 1.5 5.5 2 6H4c.5-.5 2-2 2-6Z"/><path d="M10 19a2 2 0 0 0 4 0"/></svg>',
  sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg>',
  shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m12 3-8 3v5c0 5 3.4 8.3 8 10 4.6-1.7 8-5 8-10V6Z"/><path d="m9 12 2 2 4-5"/></svg>',
  arrow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="m9 18 6-6-6-6"/></svg>',
  github: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 2a10 10 0 0 0-3.16 19.49c.5.09.68-.22.68-.48v-1.7c-2.78.6-3.37-1.34-3.37-1.34-.46-1.16-1.11-1.47-1.11-1.47-.91-.62.07-.6.07-.6 1 .07 1.53 1.03 1.53 1.03.9 1.53 2.36 1.09 2.93.83.09-.65.35-1.09.63-1.34-2.22-.25-4.56-1.11-4.56-4.94 0-1.09.39-1.99 1.03-2.69-.1-.25-.45-1.27.1-2.64 0 0 .84-.27 2.75 1.03a9.6 9.6 0 0 1 5 0c1.91-1.3 2.75-1.03 2.75-1.03.55 1.37.2 2.39.1 2.64.64.7 1.03 1.6 1.03 2.69 0 3.84-2.34 4.68-4.57 4.93.36.31.68.92.68 1.85v2.75c0 .26.18.58.69.48A10 10 0 0 0 12 2Z"/></svg>',
  calendar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 10h18"/></svg>',
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/></svg>',
  mail: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>',
  discord: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M7 8c-2 .6-3 3-3 7.5 1.5 1.2 3 1.8 4.5 2l.7-1.3c-.7-.2-1.3-.5-1.9-.9.2-.1.3-.2.5-.3 3.2 1.4 6.8 1.4 10 0l.5.3c-.6.4-1.2.7-1.9.9l.7 1.3c1.5-.2 3-.8 4.5-2 0-4.5-1-7-3-7.5-.9-.3-1.9-.5-2.9-.6l-.4.9a13 13 0 0 0-4.8 0l-.4-.9c-1 .1-2 .3-2.9.6Z"/><circle cx="9.5" cy="13" r="1"/><circle cx="14.5" cy="13" r="1"/></svg>',
  clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  lock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>',
  user: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>',
};

function mountIcons(root) {
  qsa("[data-icon]", root).forEach((el) => {
    if (!el.dataset.mounted) {
      el.innerHTML = ICONS[el.dataset.icon] || "";
      el.dataset.mounted = "1";
    }
  });
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtDuration(fromIso, toIso) {
  const from = new Date(fromIso).getTime();
  const to = toIso ? new Date(toIso).getTime() : Date.now();
  if (Number.isNaN(from)) return "—";
  const s = Math.max(0, Math.round((to - from) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

// --------------------------------------------------------------- session

// bump this whenever an icon file under /assets is replaced in place, so
// browsers that cached the old bytes at the same URL fetch the new ones
const ASSET_V = "4";

const TOKEN_KEY = "aether_token";
const COLLAPSE_KEY = "aether_sidebar_collapsed";
const THEME_KEY = "aether_theme";
const token = () => localStorage.getItem(TOKEN_KEY) || "";
const hasToken = () => Boolean(token());

async function api(path, opts) {
  const sep = path.includes("?") ? "&" : "?";
  const res = await fetch(`${path}${sep}token=${encodeURIComponent(token())}`, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail || `request failed (${res.status})`);
  }
  if (res.status === 204) return null;
  return res.json();
}

// --------------------------------------------------------------- toasts

function toast(message, kind) {
  const box = $("toasts");
  const node = h("div", { class: `toast ${kind === "err" ? "err" : "ok"}`, text: message });
  box.appendChild(node);
  setTimeout(() => node.remove(), 4500);
}

// --------------------------------------------------------------- state

const state = {
  page: "activity",
  approvals: [],
  events: [],
  audit: { entries: [], chain: null },
  policy: null,
  tasks: [],
  taskFilter: "all",
  people: [],
  approvalUi: {}, // id -> { pending, result: {ok, msg} }
  traces: [],
  tracesError: "", // set when the feed itself fails (e.g. not wired)
  tracesOpen: new Set(), // expanded trace ids
  traceDetails: {}, // id -> fetched payload-bearing trace
};

// --------------------------------------------------------------- polling

let pollHandles = [];
function stopPolling() { pollHandles.forEach(clearInterval); pollHandles = []; }

async function refreshApprovals() {
  if (!hasToken()) return;
  try {
    const data = await api("/api/approvals");
    state.approvals = data.pending;
  } catch (err) {
    return; // transient network hiccup — keep the last known list on screen
  }
  const count = state.approvals.length;
  const badge = $("nav-attention-count");
  badge.hidden = count === 0;
  badge.textContent = String(count);
  const bellCount = $("bell-count");
  bellCount.hidden = count === 0;
  bellCount.textContent = String(count);
  if (state.page === "attention") paintAttention();
}

async function refreshEvents() {
  if (!hasToken()) return;
  try {
    const data = await api("/api/events?limit=100");
    state.events = data.events;
  } catch { return; }
  if (state.page === "activity") paintActivity();
}

async function refreshAudit() {
  if (!hasToken()) return;
  try {
    const data = await api("/api/audit?limit=200");
    state.audit = data;
  } catch { return; }
  if (state.page === "audit") paintAudit();
  if (state.page === "attention") paintAttention(); // the recently-decided strip reads the same feed
}

async function refreshTraces() {
  if (!hasToken()) return;
  try {
    const data = await api("/api/traces?limit=50");
    state.traces = data.traces;
    state.tracesError = "";
  } catch (err) {
    state.tracesError = err.message; // e.g. not wired — the page says so honestly
    return;
  }
  if (state.page === "traces") paintTraces();
}

function startPolling() {
  stopPolling();
  refreshApprovals(); refreshEvents(); refreshAudit(); refreshTraces();
  pollHandles.push(setInterval(refreshApprovals, 5000));
  pollHandles.push(setInterval(refreshEvents, 10000));
  pollHandles.push(setInterval(refreshAudit, 10000));
  pollHandles.push(setInterval(refreshTraces, 10000));
}

// --------------------------------------------------------------- shared bits

function noTokenNotice() {
  return h("div", { class: "empty-state" },
    h("div", { class: "title", text: "No API token set" }),
    h("div", { class: "sub" }, "Add your API token in ",
      h("a", { href: "#/settings", style: "color:var(--primary)" }, "Settings"),
      " to connect."));
}

function emptyState(title, sub) {
  return h("div", { class: "empty-state" }, h("div", { class: "title", text: title }), h("div", { class: "sub", text: sub || "" }));
}

function cardWrap(node) {
  return h("div", { class: "card" }, node);
}

function pageHead(eyebrow, title, sub) {
  return [
    h("div", { class: "eyebrow" }, eyebrow),
    h("div", { class: "section-title" }, title),
    h("div", { class: "section-sub" }, sub),
  ];
}

function approvalCard(a) {
  const ui = state.approvalUi[a.id] || {};
  const details = paramRows(a.params);
  // why the gate parked this one — the ruling's own reason, in plain words
  const why = describeOutcome(a.note) || describeRule(a.rules_matched);
  const card = h("div", { class: "approval-card" },
    h("div", { class: "spread" },
      h("span", { class: "tool", text: describeTool(a.tool_name) }),
      h("span", { class: "badge pending", text: "waiting for you" })),
    h("div", { class: "meta", style: "color:var(--text-3);font-size:11px;margin-top:4px" },
      `asked ${fmtTime(a.created_at)} · expires ${fmtTime(a.expires_at)}`),
    why ? h("div", { class: "approval-why", text: why }) : "",
    details.length
      ? h("dl", { class: "approval-details" }, ...details.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]))
      : "");
  const actions = h("div", { class: "actions" },
    h("button", { class: "btn approve", disabled: ui.pending || undefined, onclick: () => decide(a.id, "approve") }, "Approve"),
    h("button", { class: "btn deny", disabled: ui.pending || undefined, onclick: () => decide(a.id, "deny") }, "Deny"));
  card.appendChild(actions);
  if (ui.pending) card.appendChild(h("div", { class: "result", text: "sending decision…" }));
  if (ui.result) card.appendChild(h("div", { class: `result ${ui.result.ok ? "ok" : "err"}`, text: ui.result.msg }));
  return card;
}

async function decide(id, decision) {
  state.approvalUi[id] = { pending: true };
  if (state.page === "attention") paintAttention();
  try {
    const res = await api(`/api/approvals/${id}/decide`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ decision }),
    });
    state.approvalUi[id] = { pending: false, result: res.ok
      ? { ok: true, msg: decision === "approve" ? "Approved — Aether will go ahead." : "Denied — Aether won't do this." }
      : { ok: false, msg: res.reason === "already decided or expired" ? "This request was already handled or has expired." : (res.reason || "Your decision wasn't applied.") } };
  } catch (err) {
    state.approvalUi[id] = { pending: false, result: { ok: false, msg: err.message } };
  }
  if (state.page === "attention") paintAttention();
  // give the user a beat to actually see the confirmation before the next
  // refetch removes the card (an immediate refetch made it disappear before
  // a single frame ever painted it — caught by the e2e approve test).
  setTimeout(refreshApprovals, 1200);
}

function eventListItem(e) {
  const { title, detail } = describeEvent(e);
  return h("div", { class: "list-item" },
    h("span", { class: "time mono", text: fmtTime(e.at) }),
    h("div", { class: "body" },
      h("div", { class: "k" },
        h("span", { class: "badge idle", text: appLabel(e.source) }),
        " ", title,
        e.memorable ? h("span", { class: "badge salient", style: "margin-left:6px", text: "important" }) : ""),
      detail ? h("div", { class: "meta", text: detail }) : ""));
}

function shortHash(hash) {
  if (!hash) return "—";
  return hash.length > 14 ? `${hash.slice(0, 6)}…${hash.slice(-6)}` : hash;
}

const ACTOR_LABELS = { agent: "Aether", scheduler: "Scheduled check", system: "Aether (automatic)", user: "You" };

const AUDIT_DECISIONS = {
  allow: "Allowed", approve: "Asked you", require_approval: "Asked you",
  deny: "Blocked", filtered: "Filtered out", info: "Noted",
  error: "Failed before the gate",
};

const OUTCOME_LABELS = {
  "parked for approval": "Waiting for your OK",
  "executed after approval": "Done after you approved",
  "could not run after approval": "Couldn't run after you approved",
  "approval expired": "Expired without a decision",
  "stored encrypted — value never recorded": "Saved an app key (the value was never recorded)",
  "entity note added": "Saved a note about someone",
  "identities merged": "Recognised two accounts as one person",
  "new identity created": "Met someone new",
  "action scheduled": "Scheduled for later",
  "source poll": "Regular check-in",
  "filtered by contact allowlist": "Sender isn't on your contact list",
  "read-only tool, no side effects": "Only looked, nothing changed",
  "internal tool, touches only Aether's own state": "Only touched Aether's own memory",
  "reaches an external system or is hard to undo": "Needs your OK — it changes something outside Aether",
  "unknown tool — held for a human decision": "Unfamiliar action — held for you",
  "changes who Aether listens to, what it may do, or what it's connected to — the user decides that with one tap": "Changes who Aether listens to or what it's connected to — your call",
  "an unrecognized config path can never apply silently — held for the user to look at": "An unrecognized setting — held for you to look at",
};

function describeRule(rule) {
  if (!rule) return "";
  if (BUILTIN_PLAIN[rule]) return BUILTIN_PLAIN[rule][0];
  if (rule === "config:poll_tools") return "Your regular check-ins";
  if (rule.startsWith("user:")) return `Your rule: ${describeToolPattern(rule.slice(5)).toLowerCase()}`;
  if (rule.startsWith("routine:")) return "One of your routines";
  return "";
}

function describeOutcome(outcome) {
  if (!outcome) return "";
  if (OUTCOME_LABELS[outcome]) return OUTCOME_LABELS[outcome];
  const decided = /^(approved|denied) by (\w+)$/.exec(outcome);
  if (decided) return `${decided[1] === "approved" ? "Approved" : "Denied"} by you on ${appLabel(decided[2])}`;
  if (outcome.startsWith("handle linked")) return "Linked an account to someone you know";
  return outcome[0].toUpperCase() + outcome.slice(1);
}

function auditRow(entry) {
  const action = describeTool(entry.tool);
  const tr = h("tr", { onclick: () => toast(`#${entry.seq} · ${action}`, "ok") });
  const cell = (label, node) => { const td = h("td", { "data-label": label }); td.appendChild(node); tr.appendChild(td); return td; };
  cell("Seq", h("span", { class: "mono", text: `#${entry.seq}` }));
  cell("Time", h("span", { class: "mono", text: fmtTime(entry.at) }));
  cell("Who", h("span", { text: ACTOR_LABELS[entry.actor] || entry.actor }));
  cell("Action", h("span", { text: action }));
  cell("Decision", h("span", { class: `badge ${entry.decision}`, text: AUDIT_DECISIONS[entry.decision] || entry.decision }));
  cell("Why", h("span", { style: "font-size:11px;color:var(--text-3)", text: describeRule(entry.rules_matched) }));
  cell("Result", h("span", { style: "font-size:11px;color:var(--text-2)", text: describeOutcome(entry.outcome) }));
  cell("Chain", h("span", { class: "mono", style: "font-size:11px;color:var(--text-3)", text: shortHash(entry.hash) }));
  return tr;
}

// --------------------------------------------------------------- pages

const PAGE_TITLES = {
  activity: "Live Activity",
  attention: "Attention Required",
  tasks: "Tasks",
  memory: "Memory & Context",
  audit: "Audit Log",
  traces: "Decision Traces",
  policy: "Authorization & Policies",
  apps: "Apps & Permissions",
  settings: "Settings",
};

function renderActivity() {
  const page = $("page");
  page.innerHTML = "";
  page.appendChild(h("div", {},
    ...pageHead("What's happening", "Live Activity", "What Aether has seen and done across your apps, newest first.")));
  if (!hasToken()) { page.appendChild(cardWrap(noTokenNotice())); return; }
  page.appendChild(h("div", {},
    h("div", { class: "filters" },
      h("select", { class: "field", id: "f-source" }, h("option", { value: "" }, "All apps")),
      h("input", { class: "field", id: "f-search", type: "search", placeholder: "search activity…" }),
      h("label", { class: "row", style: "font-size:12px;color:var(--text-2)" },
        h("input", { type: "checkbox", id: "f-salient" }), " important only")),
    h("div", { class: "card", id: "activity-list" })));
  $("f-source").addEventListener("change", paintActivity);
  $("f-search").addEventListener("input", paintActivity);
  $("f-salient").addEventListener("change", paintActivity);
  paintActivity();
  ensurePeople().then(() => { if (state.page === "activity") paintActivity(); });
}

let peopleLoaded = false;
async function ensurePeople() {
  if (peopleLoaded || !hasToken()) return;
  try {
    state.people = (await api("/api/memory")).people;
    peopleLoaded = true;
  } catch { /* names fall back to handles */ }
}

function paintActivity() {
  const list = $("activity-list");
  if (!list) return;

  const sourceSel = $("f-source");
  const priorSource = sourceSel.value;
  const sources = Array.from(new Set(state.events.map((e) => e.source))).sort();
  sourceSel.innerHTML = "";
  sourceSel.appendChild(h("option", { value: "" }, "All apps"));
  sources.forEach((s) => sourceSel.appendChild(h("option", { value: s }, appLabel(s))));
  sourceSel.value = sources.includes(priorSource) ? priorSource : "";

  const search = $("f-search").value.trim().toLowerCase();
  const salientOnly = $("f-salient").checked;
  const filtered = state.events.filter((e) => {
    if (sourceSel.value && e.source !== sourceSel.value) return false;
    if (salientOnly && !e.memorable) return false;
    if (search) {
      const { title, detail } = describeEvent(e);
      if (!`${title} ${detail} ${appLabel(e.source)}`.toLowerCase().includes(search)) return false;
    }
    return true;
  });
  list.innerHTML = "";
  if (!state.events.length) { list.appendChild(emptyState("Aether is observing.", "No new events have been detected.")); return; }
  if (!filtered.length) { list.appendChild(emptyState("No events match these filters.", "Try clearing a filter above.")); return; }
  filtered.forEach((e) => list.appendChild(eventListItem(e)));
}

function renderAttention() {
  const page = $("page");
  page.innerHTML = "";
  page.appendChild(h("div", {},
    ...pageHead("Your decision", "Attention Required", "Things Aether wants to do that need your OK first."),
    h("div", { class: "approval-layout" },
      h("div", { id: "attention-list" }),
      h("div", { class: "card policy-aside", id: "attention-policy" }, h("div", { class: "skeleton", style: "height:60px" })))));
  paintAttention();
  loadAttentionPolicy();
}

async function loadAttentionPolicy() {
  const box = $("attention-policy");
  if (!box) return;
  if (!hasToken()) { box.remove(); return; }
  try {
    const data = state.policy && state.policy.rules ? state.policy : await api("/api/policy");
    state.policy = data;
    box.innerHTML = "";
    box.appendChild(h("h2", {}, "Approval policy"));
    const graphic = h("div", { class: "trust-graphic" },
      h("span", { class: "ring r1" }), h("span", { class: "ring r2" }), h("span", { "data-icon": "shield" }));
    box.appendChild(graphic);
    mountIcons(graphic);
    box.appendChild(h("p", {},
      "Looking things up happens on its own. Anything that sends, changes or deletes something waits here for your OK."));
    box.appendChild(h("div", { class: "mini-rule" },
      h("span", {}, "Your own rules"), h("b", { text: String(data.rules.length) })));
    box.appendChild(h("div", { class: "mini-rule" },
      h("span", {}, "Requests expire after"), h("b", { text: `${data.approval_ttl_hours} hours` })));
    box.appendChild(h("a", { class: "text-button", href: "#/policy" }, "View full policy ", h("span", { "data-icon": "arrow" })));
    mountIcons(box);
  } catch (err) {
    box.innerHTML = "";
    box.appendChild(emptyState("Could not load policy.", err.message));
  }
}

// audit outcomes that close an approval's story — the "recently decided"
// strip is carved out of the already-polled audit feed, no extra endpoint
const DECIDED_OUTCOME = /^(approved|denied) by \w+$/;
const SETTLED_OUTCOMES = new Set([
  "executed after approval", "could not run after approval", "approval expired",
]);

function decidedRow(entry) {
  return h("div", { class: "decided-row" },
    h("span", { class: "time mono", text: fmtTime(entry.at) }),
    h("span", { class: "what", text: describeTool(entry.tool) }),
    h("span", { class: "how", text: describeOutcome(entry.outcome) }));
}

function paintAttention() {
  const list = $("attention-list");
  if (!list) return;
  list.innerHTML = "";
  if (!hasToken()) { list.appendChild(cardWrap(noTokenNotice())); return; }
  if (!state.approvals.length) {
    list.appendChild(h("div", { class: "card" }, emptyState("Nothing requires your attention.", "Aether is operating within its current authorization boundaries.")));
  } else {
    const card = h("div", { class: "card" });
    state.approvals.forEach((a) => card.appendChild(approvalCard(a)));
    list.appendChild(card);
  }
  // what became of earlier asks — so a decided card never just vanishes
  const settled = state.audit.entries
    .filter((e) => DECIDED_OUTCOME.test(e.outcome || "") || SETTLED_OUTCOMES.has(e.outcome))
    .slice(0, 5);
  if (settled.length) {
    list.appendChild(h("div", { class: "section-label", style: "margin-top:16px" },
      h("span", {}, "Recently decided")));
    const box = h("div", { class: "card" });
    settled.forEach((e) => box.appendChild(decidedRow(e)));
    list.appendChild(box);
  }
}

function renderAudit() {
  const page = $("page");
  page.innerHTML = "";
  page.appendChild(h("div", {},
    ...pageHead("Verifiability", "Audit Log", "Every decision Aether's policy made, in a tamper-evident hash chain.")));
  if (!hasToken()) { page.appendChild(cardWrap(noTokenNotice())); return; }
  page.appendChild(h("div", {},
    h("div", { id: "audit-chain" }),
    h("div", { class: "filters" },
      h("select", { class: "field", id: "f-decision" },
        h("option", { value: "" }, "All decisions"),
        h("option", { value: "allow" }, "Allowed"),
        h("option", { value: "approve" }, "Asked you"),
        h("option", { value: "deny" }, "Blocked"),
        h("option", { value: "filtered" }, "Filtered out"),
        h("option", { value: "info" }, "Noted")),
      h("input", { class: "field", id: "a-search", type: "search", placeholder: "search actions…" })),
    h("div", { class: "card" },
      h("table", { class: "data" },
        h("thead", {}, h("tr", {},
          h("th", {}, "Seq"), h("th", {}, "Time"), h("th", {}, "Who"), h("th", {}, "Action"),
          h("th", {}, "Decision"), h("th", {}, "Why"), h("th", {}, "Result"), h("th", {}, "Chain"))),
        h("tbody", { id: "audit-tbody" })))));
  $("f-decision").addEventListener("change", paintAudit);
  $("a-search").addEventListener("input", paintAudit);
  paintAudit();
}

function paintAudit() {
  const banner = $("audit-chain");
  const tbody = $("audit-tbody");
  if (!banner || !tbody) return;
  const chain = state.audit.chain;
  banner.innerHTML = "";
  if (chain) {
    const el = h("div", { class: `chain-banner ${chain.ok ? "ok" : "bad"}` },
      h("div", { class: "chain-icon", "data-icon": "shield" }),
      h("div", { class: "chain-body" },
        h("span", { class: `badge ${chain.ok ? "ok" : "bad"}`, text: chain.ok ? "chain integrity verified" : "chain integrity broken" }),
        h("p", {}, chain.ok
          ? `All ${chain.entries} record${chain.entries === 1 ? "" : "s"} are valid.`
          : `Broken at #${chain.first_bad_seq}: ${chain.problem || "mismatch"}`)),
      h("div", { class: "chain-hash" },
        h("span", {}, "head hash"),
        h("code", { text: shortHash(chain.head_hash) })));
    banner.appendChild(el);
    mountIcons(el);
  }
  const decisionFilter = $("f-decision").value;
  const search = $("a-search").value.trim().toLowerCase();
  const rows = state.audit.entries.filter((e) => {
    if (decisionFilter && e.decision !== decisionFilter) return false;
    if (search && !`${describeTool(e.tool)} ${describeOutcome(e.outcome)} ${describeRule(e.rules_matched)}`.toLowerCase().includes(search)) return false;
    return true;
  });
  tbody.innerHTML = "";
  if (!state.audit.entries.length) {
    tbody.appendChild(h("tr", {}, h("td", { colspan: "8" }, emptyState("No entries yet.", "Actions will appear here as Aether's policy evaluates them."))));
    return;
  }
  if (!rows.length) {
    tbody.appendChild(h("tr", {}, h("td", { colspan: "8" }, emptyState("No entries match these filters.", ""))));
    return;
  }
  rows.forEach((e) => tbody.appendChild(auditRow(e)));
}

// --------------------------------------------------------------- traces

const TRACE_KINDS = {
  turn: ["conversation", "info"],
  routine: ["routine", "active"],
  scheduled: ["scheduled", "idle"],
  carry_out: ["approved action", "warn"],
};

function renderTraces() {
  const page = $("page");
  page.innerHTML = "";
  page.appendChild(h("div", {},
    ...pageHead("The why", "Decision Traces", "What triggered each act, how the authorization gate ruled on every call, and what came back."),
    h("div", { class: "card", id: "traces-body" }, h("div", { class: "skeleton", style: "height:60px" }))));
  if (!hasToken()) {
    const box = $("traces-body"); box.innerHTML = ""; box.appendChild(noTokenNotice());
    return;
  }
  paintTraces();
  ensurePeople().then(() => { if (state.page === "traces") paintTraces(); });
}

function paintTraces() {
  const box = $("traces-body");
  if (!box) return;
  box.innerHTML = "";
  if (state.tracesError) {
    box.appendChild(emptyState("Decision traces aren't available.", state.tracesError));
    return;
  }
  if (!state.traces.length) {
    box.appendChild(emptyState("No traces yet.", "Aether records the why behind every act — turns, routines, scheduled actions and approved calls land here."));
    return;
  }
  state.traces.forEach((t) => box.appendChild(traceRow(t)));
}

function traceRow(t) {
  const [kindLabel, kindClass] = TRACE_KINDS[t.kind] || [t.kind, "idle"];
  const open = state.tracesOpen.has(t.id);
  const row = h("div", { class: `trace-row ${open ? "open" : ""}` },
    h("button", { class: "trace-head", onclick: () => toggleTrace(t.id) },
      h("span", { class: "time mono", text: fmtTime(t.at) }),
      h("span", { class: `badge ${kindClass}`, text: kindLabel }),
      h("span", { class: "trace-label", text: t.label || "(no label)" }),
      h("span", { class: "trace-caret", "data-icon": "arrow" })));
  if (open) {
    const detail = state.traceDetails[t.id];
    row.appendChild(detail
      ? traceDetail(detail)
      : h("div", { class: "trace-detail" }, h("div", { class: "skeleton", style: "height:40px" })));
  }
  mountIcons(row);
  return row;
}

async function toggleTrace(id) {
  if (state.tracesOpen.has(id)) {
    state.tracesOpen.delete(id);
    paintTraces();
    return;
  }
  state.tracesOpen.add(id);
  paintTraces();
  if (!state.traceDetails[id]) {
    try {
      state.traceDetails[id] = await api(`/api/traces/${id}`);
    } catch (err) {
      state.traceDetails[id] = { id, kind: "error", label: "", at: "", payload: { error: err.message } };
    }
    if (state.page === "traces") paintTraces();
  }
}

function traceCallRow(c) {
  const cls = c.decision === "error" ? "bad" : c.decision;
  return h("div", { class: "trace-call" },
    h("div", { class: "spread" },
      h("span", { class: "trace-call-name", text: describeTool(c.name) }),
      h("span", { class: `badge ${cls}`, text: AUDIT_DECISIONS[c.decision] || c.decision })),
    c.matched_rule && describeRule(c.matched_rule)
      ? h("div", { class: "meta", text: describeRule(c.matched_rule) }) : "",
    c.result
      ? h("div", { class: `meta ${c.is_error ? "trace-err" : ""}`, text: c.is_error ? `failed: ${quote(c.result, 160)}` : summarizeResult(c.result) || quote(c.result, 160) })
      : "",
    c.approval_id
      ? h("div", { class: "meta" }, "parked as ", h("a", { href: "#/attention", style: "color:var(--primary)" }, `approval #${c.approval_id}`))
      : "");
}

function traceDetail(t) {
  const p = t.payload || {};
  const box = h("div", { class: "trace-detail" });
  const line = (...kids) => box.appendChild(h("div", { class: "trace-line" }, ...kids));

  if (t.kind === "turn" && p.trigger) {
    (p.trigger.messages || []).forEach((m) =>
      line(h("span", { class: "badge idle", text: appLabel(m.surface) || "chat" }),
        h("span", { text: ` ${personName(m.surface, m.handle)} said: ${quote(m.text, 220)}` })));
    (p.trigger.observations || []).forEach((o) =>
      line(h("span", { class: "badge idle", text: appLabel(o.source) }),
        h("span", { class: "meta", text: ` observed ${o.kind} — ${(o.line || "").slice(0, 220)}` })));
  }
  if (t.kind === "routine" && p.routine) {
    line(h("span", { text: `Routine “${p.routine.label}” fired` }),
      p.routine.trigger ? h("span", { class: "meta", text: ` — trigger: ${p.routine.trigger}` }) : "");
    if (p.event) line(h("span", { class: "badge idle", text: appLabel(p.event.source) }),
      h("span", { class: "meta", text: ` ${p.event.kind} — ${(p.event.line || "").slice(0, 220)}` }));
  }
  if (t.kind === "scheduled" && p.action) {
    line(h("span", { text: `Scheduled action “${p.action.label}”` }),
      h("span", { class: "meta", text: ` — due ${p.action.run_at || ""}` }));
  }
  if (t.kind === "carry_out") {
    line(h("span", { text: `Carrying out approval #${p.approval_id}: ${describeTool(p.tool)}` }),
      p.decided_by ? h("span", { class: "meta", text: ` — decided by ${p.decided_by === "user" ? "you" : p.decided_by}` }) : "");
    const rows = paramRows(p.params);
    if (rows.length) box.appendChild(h("dl", { class: "approval-details" },
      ...rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])));
  }

  (p.calls || []).forEach((c) => box.appendChild(traceCallRow(c)));

  if (Array.isArray(p.reasoning) && p.reasoning.length) {
    box.appendChild(h("div", { class: "trace-sub" }, "What it thought"));
    p.reasoning.forEach((r) => box.appendChild(h("div", { class: "trace-reason", text: r })));
  }
  if (p.reply) {
    box.appendChild(h("div", { class: "trace-sub" }, "What it answered"));
    box.appendChild(h("div", { class: "trace-reply", text: p.reply }));
  }
  if (p.result !== undefined && t.kind !== "turn") {
    box.appendChild(h("div", { class: `meta ${p.is_error ? "trace-err" : ""}`, style: "margin-top:6px" },
      `${p.is_error ? "failed: " : "result: "}${quote(String(p.result), 200)}`));
  }
  if (p.error) {
    box.appendChild(h("div", { class: "meta trace-err", style: "margin-top:6px", text: `error: ${p.error}` }));
  }
  return box;
}

const APP_NAMES = {
  gmail: "Email", mail: "Email", email: "Email", outlook: "Email",
  calendar: "Calendar", gcal: "Calendar",
  slack: "Slack", telegram: "Telegram", discord: "Discord", whatsapp: "WhatsApp", teams: "Teams",
  github: "GitHub", drive: "Files", filesystem: "Files", fs: "Files",
  memory: "Aether's memory",
};

const VERBS = [
  [/^(send|forward)$/, "Sending"], [/^reply$/, "Replying to"], [/^(post|publish)$/, "Posting"],
  [/^(delete|remove)$/, "Deleting"], [/^cancel$/, "Cancelling"], [/^(pay|transfer)$/, "Paying or transferring"],
  [/^invite$/, "Inviting people to"], [/^book$/, "Booking"], [/^create$/, "Creating"],
  [/^(update|edit|modify)$/, "Changing"], [/^(write|upload)$/, "Writing"], [/^(note|remember|save)$/, "Saving"],
  [/^(read|get|fetch|list|search|find|query|watch)$/, "Reading"],
];

const NOUNS = { email: "email", emails: "email", mail: "email", message: "messages", messages: "messages",
  msg: "messages", chat: "chats", chats: "chats", event: "events", events: "events",
  file: "files", files: "files", issue: "issues", issues: "issues", note: "notes", notes: "notes",
  channel: "channels", channels: "channels", contact: "contacts", contacts: "contacts" };

const DEFAULT_NOUN = { Email: "email", Calendar: "events", Slack: "messages", Telegram: "messages",
  Discord: "messages", WhatsApp: "messages", Teams: "messages", GitHub: "issues", Files: "files" };

const NATIVE_TOOLS = {
  memory_search: "Searching its memory",
  note_entity: "Saving a note about someone",
  entity_merge: "Recognising two accounts as the same person",
  entity_resolve: "Linking an account to a person",
  get_pending_approvals: "Checking what's waiting for your OK",
  schedule_action: "Scheduling something for later",
  request_screen_capture: "Asking for a screenshot",
  send_chat_message: "Messaging you",
  get_config: "Reading its own configuration",
  set_config: "Changing its own configuration",
  explain_decision: "Explaining a past decision",
  verify_integrity: "Verifying the audit chain",
  create_routine: "Learning a routine",
  list_routines: "Listing its routines",
  set_routine_enabled: "Turning a routine on or off",
  delete_routine: "Forgetting a routine",
};

const EXTRA_SOURCES = { screen: "Screen", web: "Web", user: "You" };

function appLabel(source) {
  if (!source) return "";
  const key = String(source).toLowerCase();
  return APP_NAMES[key] || EXTRA_SOURCES[key] || key[0].toUpperCase() + key.slice(1);
}

function describeTool(name) {
  if (!name) return "An action";
  if (NATIVE_TOOLS[name]) return NATIVE_TOOLS[name];
  if (name.startsWith("ingest:")) return `Receiving a message on ${appLabel(name.slice(7))}`;
  return describeName(name);
}

function personName(platform, handle) {
  if (!handle) return "someone";
  if (handle === "user") return "you";
  const match = state.people.find((p) => p.handles.some((hd) => hd.handle === handle && (!platform || hd.platform === platform)));
  return match ? match.display_name : handle;
}

const PARAM_LABELS = {
  to: "To", cc: "Cc", recipient: "To", recipients: "To", chat: "Chat", channel: "Channel", handle: "To",
  subject: "Subject", title: "Title", body: "Message", text: "Message", message: "Message", content: "Message",
  start: "When", when: "When", run_at: "When", end: "Until", date: "Date", location: "Where",
  attendees: "People", amount: "Amount", note: "Note", query: "Looking for",
};

function friendlyValue(value) {
  if (value === null || value === undefined || value === "") return "";
  if (Array.isArray(value)) return value.map(friendlyValue).filter(Boolean).join(", ");
  if (typeof value === "object") return Object.values(value).map(friendlyValue).filter(Boolean).join(" · ");
  const text = String(value);
  if (/^\d{4}-\d{2}-\d{2}T/.test(text)) return new Date(text).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  return text.length > 280 ? `${text.slice(0, 280)}…` : text;
}

function paramRows(params) {
  return Object.entries(params || {})
    .filter(([k, v]) => !k.startsWith("_") && friendlyValue(v))
    .map(([k, v]) => [PARAM_LABELS[k.toLowerCase()] || k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()), friendlyValue(v)]);
}

function quote(text, max = 200) {
  const clean = String(text).replace(/\s+/g, " ").trim();
  return `“${clean.length > max ? `${clean.slice(0, max)}…` : clean}”`;
}

function summarizeResult(result) {
  if (!result) return "";
  let parsed = null;
  try { parsed = JSON.parse(result); } catch { /* plain text */ }
  if (Array.isArray(parsed)) return parsed.length ? `Found ${parsed.length} new item${parsed.length === 1 ? "" : "s"}` : "Nothing new";
  if (parsed && typeof parsed === "object") {
    const who = parsed.from || parsed.sender || parsed.organizer;
    const what = parsed.subject || parsed.title || parsed.summary || parsed.text;
    return [who ? `from ${personName("", who)}` : "", what ? quote(what, 140) : ""].filter(Boolean).join(" — ");
  }
  return quote(result, 140);
}

function describeEvent(e) {
  const p = e.payload || {};
  const app = appLabel(e.source);
  const sender = p._sender || (p.handle ? { platform: e.source, handle: p.handle } : null);
  if (e.kind === "chat_message") {
    const who = sender ? personName(sender.platform, sender.handle) : "someone";
    return { title: who === "you" ? `You wrote on ${app}` : `Chat with ${who} on ${app}`, detail: p.text ? quote(p.text) : "" };
  }
  if (e.kind === "screen_capture") {
    const people = Array.isArray(p.people) && p.people.length ? ` · people: ${p.people.join(", ")}` : "";
    return { title: `Looked at your screen${p.app ? ` — ${p.app}` : ""}`, detail: `${p.summary || p.user_note || ""}${people}` };
  }
  if (e.kind.startsWith("poll:")) {
    const noun = DEFAULT_NOUN[app] || "updates";
    const detail = p.result !== undefined ? summarizeResult(p.result)
      : [p.from ? `from ${personName(e.source, p.from)}` : "", p.subject || p.title ? quote(p.subject || p.title, 140) : ""].filter(Boolean).join(" — ");
    const title = app === "Email" || app === "Calendar" ? `Checked your ${app.toLowerCase()}` : `Checked ${app} for new ${noun}`;
    return { title, detail };
  }
  const words = e.kind.replace(/[_:]+/g, " ").trim();
  const fields = paramRows(p).map(([k, v]) => `${k}: ${v}`).join(" · ");
  return { title: `${words[0].toUpperCase()}${words.slice(1)} on ${app}`, detail: fields };
}

function cleanPattern(pattern) {
  return pattern.replace(/^\^|\$$/g, "").replace(/\\b/g, "").replace(/\\/g, "");
}

function describeName(name) {
  const wildcard = /[.*+?\[\]]/;
  let [app, action] = name.includes("__") ? name.split("__") : ["", name];
  const appName = app && !wildcard.test(app) ? (APP_NAMES[app.toLowerCase()] || app[0].toUpperCase() + app.slice(1)) : "";
  if (!action || wildcard.test(action) && !/[a-z]{3,}/i.test(action.replace(/[.*+?\[\]]/g, ""))) {
    return appName ? `Anything in ${appName}` : "Any action";
  }
  const words = action.replace(/[.*+?\[\]]/g, " ").toLowerCase().split(/[_\s]+/).filter(Boolean);
  const verbHit = words.map((w) => VERBS.find(([re]) => re.test(w))).find(Boolean);
  const noun = words.map((w) => NOUNS[w]).find(Boolean) || (appName && DEFAULT_NOUN[appName]) || "";
  if (!verbHit) {
    const phrase = words.join(" ");
    return appName ? `${phrase[0].toUpperCase()}${phrase.slice(1)} in ${appName}` : `${phrase[0].toUpperCase()}${phrase.slice(1)}`;
  }
  const verb = verbHit[1];
  const object = noun || "anything";
  const redundant = (appName === "Email" && noun === "email") || (appName === "Files" && noun === "files");
  return appName && !redundant ? `${verb} ${object} on ${appName}` : `${verb} ${object}`;
}

function describeToolPattern(pattern) {
  const cleaned = cleanPattern(pattern).replace(/^\((.*)\)$/, "$1");
  const group = /^([^()|]*)\(([^()]+)\)([^()|]*)$/.exec(cleaned);
  const parts = group ? group[2].split("|").map((alt) => group[1] + alt + group[3]) : cleaned.split("|");
  return parts.map((part) => describeName(part.replace(/[()]/g, "")))
    .map((text, i) => (i ? text[0].toLowerCase() + text.slice(1) : text)).join(" or ");
}

const DECISION_LABELS = {
  allow: ["Runs on its own", "allow"],
  approve: ["Asks you first", "require_approval"],
  require_approval: ["Asks you first", "require_approval"],
  deny: ["Never allowed", "deny"],
};

const BUILTIN_PLAIN = {
  "builtin:config-tune": ["Personal tuning from chat", "A settings change you drove in chat (agent, salience or provider settings) — it applies right away."],
  "builtin:config-security": ["A security-shaped settings change", "A change to your contacts, authorization, messaging or app servers — it always waits for your one-tap OK."],
  "builtin:internal": ["Keeping its own notes", "Remembering people and details, setting reminders, and asking for a screenshot only touch Aether's own memory."],
  "builtin:risky": ["Anything that reaches the outside world", "Sending, replying, posting, deleting, cancelling, paying, booking, inviting or creating something always waits for your OK."],
  "builtin:read-only": ["Looking things up", "Reading, searching and listing your mail, chats and calendar — nothing gets changed."],
  "builtin:walk-connect": ["Connecting an app from the /apps walk", "You walked through connecting this app in chat — the walk itself was your OK."],
  "builtin:oauth-consent": ["Connecting an app by signing in", "You approved the sign-in on the provider's own screen — that click was the OK."],
  "default:fail-safe": ["Anything unfamiliar", "If Aether doesn't recognise an action, it asks you before doing it."],
};

function renderPolicy() {
  const page = $("page");
  page.innerHTML = "";
  page.appendChild(h("div", {},
    ...pageHead("Read only", "Authorization & Policies", "What Aether may do on its own, and what always needs your OK."),
    h("div", { id: "policy-body" })));
  loadPolicy();
}

async function loadPolicy() {
  const body = $("policy-body");
  if (!hasToken()) { body.appendChild(cardWrap(noTokenNotice())); return; }
  body.innerHTML = '<div class="skeleton" style="height:80px"></div>';
  try {
    state.policy = await api("/api/policy");
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(emptyState("Could not load the policy.", err.message));
    return;
  }
  body.innerHTML = "";
  const p = state.policy;
  body.appendChild(h("div", { class: "card", style: "margin-bottom:16px" },
    h("h3", {}, "Your rules — checked first"),
    p.rules.length
      ? h("div", {}, ...p.rules.map((r, i) => {
          const [label, cls] = DECISION_LABELS[r.decision] || [r.decision, r.decision];
          return h("div", { class: "policy-rule row" },
            h("span", { class: "policy-order", text: String(i + 1) }),
            h("div", { style: "flex:1" },
              h("div", { class: "pattern", style: "font-family:inherit" }, describeToolPattern(r.tool_pattern),
                r.param_pattern ? h("span", { style: "color:var(--text-3)" }, " · only in certain cases") : ""),
              r.note ? h("div", { class: "note", text: r.note }) : ""),
            h("span", { class: `badge ${cls}`, text: label }));
        }))
      : emptyState("No custom rules yet.", "Aether follows the standard rules below.")));
  const ladder = h("div", {});
  p.builtin_rules.forEach((r, i) => {
    const [title, description] = BUILTIN_PLAIN[r.id] || [r.id, r.description];
    const [label] = DECISION_LABELS[r.decision] || [r.decision];
    ladder.appendChild(h("div", { class: `ladder-step ${r.decision}` },
      h("span", { text: String(i + 1).padStart(2, "0") }),
      h("div", {}, h("b", { text: `${title} — ${label.toLowerCase()}` }), h("small", { text: description }))));
    if (i < p.builtin_rules.length - 1) ladder.appendChild(h("i", { class: "ladder-line" }));
  });
  body.appendChild(h("div", { class: "card" }, h("h3", {}, "Standard rules"), ladder));
  body.appendChild(h("div", { style: "margin-top:12px;color:var(--text-3);font-size:12px" },
    `Requests waiting for your OK expire after ${p.approval_ttl_hours} hours.`));
}

function renderSettings() {
  const page = $("page");
  page.innerHTML = "";
  page.appendChild(h("div", { class: "stack", style: "max-width:720px" },
    ...pageHead("Local configuration", "Settings", "Configuration Aether actually persists and uses."),
    h("div", { class: "card" },
      h("h2", {}, "API token"),
      h("div", { style: "color:var(--text-2);font-size:12px;margin-bottom:10px" },
        "Required to reach the chat, approvals, activity, audit and policy endpoints. Stored only in this browser's local storage."),
      h("div", { class: "row" },
        h("input", { class: "field", id: "s-token", type: "password", placeholder: "API token", value: token(), style: "flex:1" }),
        h("button", { class: "btn primary", id: "s-save" }, "Save"),
        h("button", { class: "btn ghost", id: "s-clear" }, "Clear"))),
    h("div", { class: "card" },
      h("h2", {}, "Personality"),
      h("div", { style: "color:var(--text-2);font-size:12px;margin-bottom:10px" },
        "Free text appended to Aether's real system prompt on every turn — this genuinely changes how it behaves, not just how it looks. Leave empty for the default behavior."),
      h("textarea", {
        id: "s-personality", class: "field", rows: "5", style: "width:100%;resize:vertical;font-family:var(--font-mono)",
        placeholder: "e.g. Act like a super AI — confident, sharp, a step ahead. Be terse and a little dry. Never use emoji.",
      }),
      h("div", { class: "row", style: "margin-top:10px" },
        h("button", { class: "btn primary", id: "s-personality-save" }, "Save personality"),
        h("span", { id: "s-personality-status", style: "font-size:11px;color:var(--text-3)" }))),
    h("div", { class: "card" },
      h("h2", {}, "Configuration"),
      h("div", { style: "color:var(--text-2);font-size:12px;margin-bottom:10px" },
        "What Aether is running with — config.yaml merged with changes made in chat. Read-only here; change it from chat with /config, under the same one-tap gate as everything else."),
      h("div", { id: "s-config" }, h("div", { class: "skeleton", style: "height:60px" })))));
  $("s-save").addEventListener("click", () => {
    const val = $("s-token").value.trim();
    if (!val) { toast("enter a token first", "err"); return; }
    localStorage.setItem(TOKEN_KEY, val);
    location.reload();
  });
  $("s-clear").addEventListener("click", () => {
    localStorage.removeItem(TOKEN_KEY);
    location.reload();
  });
  if (hasToken()) {
    loadPersonality();
    loadConfig();
  } else {
    $("s-personality").disabled = true;
    $("s-personality").placeholder = "set your API token above first";
    $("s-personality-save").disabled = true;
    const cfg = $("s-config");
    cfg.innerHTML = "";
    cfg.appendChild(h("div", { class: "empty-state" }, h("div", { class: "sub", text: "set your API token above first" })));
  }
}

async function loadConfig() {
  const box = $("s-config");
  if (!box) return;
  try {
    const data = await api("/api/config");
    const s = data.sections || {};
    box.innerHTML = "";
    const messagingOn = ["telegram", "discord", "slack"]
      .filter((p) => s.messaging && s.messaging[p] && s.messaging[p].enabled);
    const servers = (s.mcp_servers || []).filter((x) => x.enabled);
    const summary = [
      ["Messaging", messagingOn.length ? messagingOn.join(", ") : "all off"],
      ["App servers (MCP)", servers.length ? servers.map((x) => x.name).join(", ") : "none"],
      ["Contact allowlist", (s.contacts && s.contacts.mode) || "off"],
      ["Approval window", s.authz ? `${s.authz.approval_ttl_hours} hours` : "—"],
      ["Quiet hours", (s.agent && s.agent.quiet_hours) || "none"],
      ["Provider", s.llm ? `${s.llm.provider} · ${s.llm.model}` : "—"],
    ];
    box.appendChild(h("dl", { class: "approval-details" },
      ...summary.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: String(v) })])));
    if (data.overrides && data.overrides.length) {
      box.appendChild(h("div", { class: "section-label", style: "margin-top:12px" },
        h("span", {}, "Changed from chat")));
      data.overrides.forEach((o) =>
        box.appendChild(h("div", { class: "config-override mono", text: `${o.path} = ${JSON.stringify(o.value)}` })));
    }
  } catch (err) {
    box.innerHTML = "";
    box.appendChild(emptyState("Configuration isn't available.", err.message));
  }
}

async function loadPersonality() {
  const box = $("s-personality");
  const status = $("s-personality-status");
  try {
    const data = await api("/api/settings/personality");
    box.value = data.text;
  } catch (err) {
    status.textContent = `couldn't load: ${err.message}`;
    status.style.color = "var(--red)";
  }
  $("s-personality-save").addEventListener("click", async () => {
    status.textContent = "saving…";
    status.style.color = "var(--text-3)";
    try {
      await api("/api/settings/personality", {
        method: "PUT", headers: { "content-type": "application/json" },
        body: JSON.stringify({ text: box.value }),
      });
      status.textContent = "saved — takes effect on Aether's next reply";
      status.style.color = "var(--green)";
    } catch (err) {
      status.textContent = `not saved: ${err.message}`;
      status.style.color = "var(--red)";
    }
  });
}

const CONFIG_TOAST = "Change this from chat — /apps connects an app, /config changes settings.";

function brandLogo(icon, extraClass, imgSrc) {
  if (extraClass === "slack") return h("span", { class: "brand-logo slack" }, h("i", {}), h("i", {}), h("i", {}), h("i", {}));
  if (imgSrc) return h("img", { class: "brand-logo", src: imgSrc, alt: "" });
  return h("span", { class: `brand-logo ${extraClass || ""}`, "data-icon": icon || "apps" });
}

function integrationCard({ icon, iconClass, imgSrc, name, subtitle, enabled, permission, statusText, badgeClass }) {
  return h("div", { class: `integration-card ${enabled ? "" : "disabled"}` },
    brandLogo(icon, iconClass, imgSrc),
    h("div", {}, h("div", { role: "heading" }, name), h("p", {}, subtitle)),
    h("button", {
      class: `switch ${enabled ? "on" : ""}`, title: `${name} — ${CONFIG_TOAST}`,
      onclick: () => toast(CONFIG_TOAST, "ok"),
    }, h("i", {})),
    h("div", { class: "integration-permission" }, h("span", { "data-icon": "lock" }), h("span", { text: permission })),
    h("div", { class: "integration-foot" },
      h("span", { class: `badge ${badgeClass || (enabled ? "ok" : "idle")}`, text: statusText || (enabled ? "connected" : "disabled") }),
      h("button", { class: "text-button", onclick: () => toast(CONFIG_TOAST, "ok") },
        "Configure ", h("span", { "data-icon": "arrow" }))));
}

// what connecting from chat looks like from the panel — the walk owns the
// keys, so the card is a signpost, not a button
function availableCard({ icon, iconClass, imgSrc, name, blurb }) {
  return h("div", { class: "available-card" },
    imgSrc ? h("img", { src: imgSrc, alt: "" }) : brandLogo(icon, iconClass),
    h("span", {}, h("b", { text: name }), h("small", { text: blurb || "" })),
    h("span", { class: "connect-hint", text: "connect: /apps in chat" }));
}

const CONNECTOR_META = {
  telegram: { imgSrc: `/assets/icon-telegram.png?v=${ASSET_V}`, subtitle: "Bot API", permission: "Read, reply, monitor" },
  discord: { imgSrc: `/assets/icon-discord.png?v=${ASSET_V}`, subtitle: "Gateway", permission: "Read selected servers" },
  slack: { iconClass: "slack", subtitle: "Socket mode", permission: "Read, search, draft" },
};

// icons for the chat-surface recipes, keyed by catalog key
const CATALOG_ICONS = {
  telegram: { imgSrc: `/assets/icon-telegram.png?v=${ASSET_V}` },
  discord: { imgSrc: `/assets/icon-discord.png?v=${ASSET_V}` },
  slack: { iconClass: "slack" },
};

// an MCP server's display face — by the name its config entry carries
const SERVER_FACES = {
  gmail: { imgSrc: `/assets/icon-gmail.png?v=${ASSET_V}`, label: "Gmail" },
  mail: { imgSrc: `/assets/icon-email.jpg?v=${ASSET_V}`, label: "Email" },
  calendar: { imgSrc: `/assets/icon-calendar.png?v=${ASSET_V}`, label: "Calendar" },
  github: { imgSrc: `/assets/icon-github.png?v=${ASSET_V}`, label: "GitHub" },
  notion: { icon: "apps", label: "Notion" },
};

function renderApps() {
  const page = $("page");
  page.innerHTML = "";
  page.appendChild(h("div", {},
    ...pageHead("Connections", "Apps & Permissions", "What Aether can actually see and act through right now."),
    h("div", { id: "apps-body" }, h("div", { class: "card" }, h("div", { class: "skeleton", style: "height:60px" })))));
  if (hasToken()) loadApps();
  else { const box = $("apps-body"); box.innerHTML = ""; box.appendChild(cardWrap(noTokenNotice())); }
}

// the /api/connections answer — null when no bridge is wired (the hub
// section says so honestly instead of failing the page)
let HUB = null;
let hubQuery = "";

async function loadApps() {
  const box = $("apps-body");
  try {
    const data = await api("/api/settings/apps");
    try { HUB = await api("/api/connections"); } catch { HUB = null; }
    const cards = [];
    // native messaging surfaces — "on" honestly means the keys are in place
    data.connectors.forEach((c) => {
      const meta = CONNECTOR_META[c.name] || { icon: "apps", subtitle: "Messaging connector", permission: "Read, reply" };
      const missingKeys = c.enabled && !c.has_token;
      cards.push(integrationCard({
        icon: meta.icon, iconClass: meta.iconClass, imgSrc: meta.imgSrc, subtitle: meta.subtitle, permission: meta.permission,
        name: c.name[0].toUpperCase() + c.name.slice(1), enabled: c.enabled,
        statusText: !c.enabled ? "disabled" : missingKeys ? "on — no token yet" : "on",
        badgeClass: !c.enabled ? "idle" : missingKeys ? "warn" : "ok",
      }));
    });
    // MCP servers with the host's live verdict — up and answering, or not yet
    data.mcp_servers.forEach((s) => {
      const face = SERVER_FACES[s.name.toLowerCase()] || { icon: "apps", label: `MCP: ${s.name}` };
      cards.push(integrationCard({
        icon: face.icon, iconClass: face.iconClass, imgSrc: face.imgSrc,
        name: face.label,
        subtitle: `MCP server · ${s.transport}`, permission: "Tools exposed by this MCP server",
        enabled: s.enabled,
        statusText: !s.enabled ? "disabled"
          : s.connected ? `connected · ${s.actions} action${s.actions === 1 ? "" : "s"}`
          : "still connecting",
        badgeClass: !s.enabled ? "idle" : s.connected ? "ok" : "warn",
      }));
    });
    cards.push(integrationCard({
      imgSrc: `/assets/icon-phone.png?v=${ASSET_V}`, name: "Screen vision", subtitle: "OCR, perception only",
      permission: "Screenshots become memory, never actions", enabled: data.screen_vision_available,
      statusText: data.screen_vision_available ? "available" : "unavailable",
    }));
    cards.push(integrationCard({
      imgSrc: `/assets/icon-monitor.png?v=${ASSET_V}`, name: "Contact allowlist", subtitle: "Ingestion filter",
      permission: "Filters senders before they reach memory", enabled: data.contacts_mode === "enforce",
      statusText: data.contacts_mode,
    }));

    box.innerHTML = "";
    box.appendChild(h("div", { class: "section-label" },
      h("span", {}, "Connected services"), h("span", { class: "badge ok", text: `${cards.length} configured` })));
    const grid = h("div", { class: "integration-grid" });
    cards.forEach((c) => grid.appendChild(c));
    box.appendChild(grid);

    // the app hub — every app Composio can connect, searchable, with the
    // connected ones carrying their identity and a disconnect
    renderHubSection(box);

    // chat surfaces the walk can still switch on — native, tokens in chat
    const connected = new Set();
    data.connectors.forEach((c) => { if (c.enabled) connected.add(c.name); });
    const addable = (data.catalog || []).filter((r) => !connected.has(r.key));
    box.appendChild(h("div", { class: "section-label upcoming" },
      h("span", {}, "Chat surfaces"), h("small", {}, "talk to Aether there — say /apps in any chat and the walk takes the tokens there")));
    const available = h("div", { class: "available-grid" });
    if (addable.length) {
      addable.forEach((r) => {
        const meta = CATALOG_ICONS[r.key] || { icon: "apps" };
        available.appendChild(availableCard({
          icon: meta.icon, iconClass: meta.iconClass, imgSrc: meta.imgSrc,
          name: r.name[0].toUpperCase() + r.name.slice(1), blurb: r.blurb,
        }));
      });
    } else {
      available.appendChild(h("div", { class: "empty-state" },
        h("div", { class: "sub" }, "Every chat surface is on.")));
    }
    box.appendChild(available);
    mountIcons(box);
  } catch (err) {
    box.innerHTML = "";
    box.appendChild(h("div", { class: "card" }, emptyState("Could not load apps & permissions.", err.message)));
  }
}

// -- the app hub: Composio's catalog, searchable, connect in one click --------------

function renderHubSection(box) {
  box.appendChild(h("div", { class: "section-label upcoming" },
    h("span", {}, "App hub"),
    h("small", {}, "every app Composio can connect — the sign-in lives at the hub, never here")));
  if (!HUB) {
    box.appendChild(h("div", { class: "card" },
      h("div", { class: "sub", style: "padding:6px 2px" }, "The app hub isn't wired on this instance.")));
    return;
  }
  if (!HUB.configured) {
    box.appendChild(h("div", { class: "card" },
      h("div", { class: "sub", style: "padding:6px 2px" },
        "No hub key yet — say /apps add <app> in chat and paste the COMPOSIO_API_KEY once. Every app after is one click.")));
    return;
  }
  if (HUB.connected.length) {
    const rows = h("div", { class: "available-grid" });
    HUB.connected.forEach((a) => rows.appendChild(hubRow(a)));
    box.appendChild(rows);
  }
  const search = h("input", { class: "field hub-search", type: "search", placeholder: "search apps…", value: hubQuery });
  search.addEventListener("input", () => { hubQuery = search.value; renderHubGrid(); });
  box.appendChild(search);
  box.appendChild(h("div", { id: "hub-grid", class: "available-grid" }));
  renderHubGrid();
  box.appendChild(h("div", { class: "hub-note" },
    "Connected means the hub holds the sign-in. What Aether may do with it is the policy gate — reads run free, sends and deletes ask first."));
}

function renderHubGrid() {
  const grid = $("hub-grid");
  if (!grid || !HUB) return;
  grid.innerHTML = "";
  const q = hubQuery.trim().toLowerCase();
  const activeSlugs = new Set(HUB.connected.filter((a) => a.status === "ACTIVE").map((a) => a.toolkit));
  const shown = HUB.toolkits.filter((t) => !activeSlugs.has(t.slug)
    && (!q || t.name.toLowerCase().includes(q) || t.slug.toLowerCase().includes(q)));
  if (!shown.length) {
    grid.appendChild(h("div", { class: "empty-state" }, h("div", { class: "sub" },
      q ? `no app matches "${q}"` : "everything the hub offers is connected.")));
    return;
  }
  shown.forEach((t) => grid.appendChild(hubCard(t)));
}

// an app's face: the hub's logo URL, a letter tile when it has none (or it 404s)
function hubLogo(toolkit, name) {
  if (toolkit && toolkit.logo) {
    const img = h("img", { src: toolkit.logo, alt: "" });
    img.addEventListener("error", () => img.replaceWith(letterTile(name)));
    return img;
  }
  return letterTile(name);
}

function letterTile(name) {
  return h("span", { class: "letter-tile", text: (name || "?").slice(0, 1).toUpperCase() });
}

function hubCard(t) {
  return h("div", { class: "available-card" },
    hubLogo(t, t.name),
    h("span", {}, h("b", { text: t.name }), h("small", { text: t.description || "connect via the app hub" })),
    h("button", { class: "btn sm", onclick: () => connectHubApp(t) }, "Connect"));
}

function hubRow(a) {
  const t = (HUB.toolkits || []).find((x) => x.slug === a.toolkit);
  const name = t ? t.name : a.toolkit;
  return h("div", { class: "available-card" },
    hubLogo(t, name),
    h("span", {}, h("b", { text: name }),
      h("small", { text: a.status === "ACTIVE" ? (a.identity || "connected") : `${a.status.toLowerCase()} — reconnect` })),
    h("button", { class: "btn sm", onclick: () => disconnectHubApp(a) }, "Disconnect"));
}

async function connectHubApp(t) {
  try {
    const r = await api(`/api/connections/${encodeURIComponent(t.slug)}/connect`, { method: "POST" });
    window.open(r.redirect_url, "_blank", "noopener");
    toast(`approve ${t.name} in the tab that just opened — Aether says in chat when it's connected`, "ok");
  } catch (err) { toast(err.message, "err"); }
}

async function disconnectHubApp(a) {
  try {
    await api(`/api/connections/${encodeURIComponent(a.id)}/disconnect`, { method: "POST" });
    toast(`disconnected ${a.toolkit}`, "ok");
    loadApps();
  } catch (err) { toast(err.message, "err"); }
}

const TASK_STATUSES = ["all", "pending", "running", "done", "failed"];

function renderTasks() {
  const page = $("page");
  page.innerHTML = "";
  page.appendChild(h("div", {},
    ...pageHead("Automation", "Tasks", "Scheduled actions — persisted, survive restarts, gated by the same authorization policy when they fire."),
    h("div", { class: "tab-row", id: "tasks-tabs" }),
    h("div", { class: "card", id: "tasks-body" }, h("div", { class: "skeleton", style: "height:60px" }))));
  if (hasToken()) loadTasks();
  else { const box = $("tasks-body"); box.innerHTML = ""; box.appendChild(noTokenNotice()); }
}

async function loadTasks() {
  const box = $("tasks-body");
  try {
    state.tasks = (await api("/api/tasks")).tasks;
    paintTasks();
  } catch (err) {
    box.innerHTML = "";
    box.appendChild(emptyState("Could not load tasks.", err.message));
  }
}

function paintTasks() {
  const tabs = $("tasks-tabs");
  const box = $("tasks-body");
  if (!tabs || !box) return;
  tabs.innerHTML = "";
  TASK_STATUSES.forEach((status) => {
    const count = status === "all" ? state.tasks.length : state.tasks.filter((t) => t.status === status).length;
    tabs.appendChild(h("button", {
      class: `btn sm ${state.taskFilter === status ? "active" : ""}`,
      onclick: () => { state.taskFilter = status; paintTasks(); },
    }, status, h("span", { class: "tab-count", text: String(count) })));
  });

  box.innerHTML = "";
  if (!state.tasks.length) {
    box.appendChild(emptyState("Nothing scheduled.", "Actions Aether schedules for later will appear here."));
    return;
  }
  const visible = state.tasks.filter((t) => state.taskFilter === "all" || t.status === state.taskFilter);
  if (!visible.length) {
    box.appendChild(emptyState("No tasks in this state.", "Try a different tab above."));
    return;
  }
  const table = h("table", { class: "data" },
    h("thead", {}, h("tr", {}, h("th", {}, "Label"), h("th", {}, "Run at"), h("th", {}, "Status"), h("th", {}, "Created"))),
    h("tbody"));
  box.appendChild(table);
  const tbody = table.querySelector("tbody");
  visible.forEach((t) => {
    const tr = h("tr", { onclick: () => toast(`${t.label}: ${describeTool(t.payload && t.payload.tool).toLowerCase()}`, "ok") });
    const cell = (label, node) => { const td = h("td", { "data-label": label }); td.appendChild(node); tr.appendChild(td); };
    cell("Label", h("span", { text: t.label }));
    cell("Run at", h("span", { class: "mono", text: fmtTime(t.run_at) }));
    cell("Status", h("span", { class: `badge ${t.status}`, text: t.status }));
    cell("Created", h("span", { class: "mono", text: fmtTime(t.created_at) }));
    tbody.appendChild(tr);
  });
}

function renderMemory() {
  const page = $("page");
  page.innerHTML = "";
  page.appendChild(h("div", {},
    ...pageHead("Context graph", "Memory & Context", "Cross-platform identities Aether has resolved, and the most recent relationship note for each."),
    h("div", { class: "memory-summary", id: "memory-summary" }),
    h("div", { class: "filters", id: "memory-filters" },
      h("input", { class: "field", id: "m-search", type: "search", placeholder: "search people…" })),
    h("div", { id: "memory-body" }, h("div", { class: "card" }, h("div", { class: "skeleton", style: "height:60px" })))));
  if (hasToken()) {
    loadMemory();
    $("m-search").addEventListener("input", paintMemory);
  } else {
    $("memory-summary").remove();
    $("memory-filters").remove();
    const box = $("memory-body"); box.innerHTML = ""; box.appendChild(cardWrap(noTokenNotice()));
  }
}

function personCard(person) {
  const card = h("div", { class: "card" },
    h("div", { class: "spread" },
      h("span", { style: "font-weight:600;font-size:14px" }, person.display_name),
      h("span", { class: "badge idle", text: `${Math.round(person.confidence * 100)}% match` })),
    h("div", { class: "row", style: "flex-wrap:wrap;margin-top:8px;gap:6px" },
      ...person.handles.map((hd) => h("span", { class: "badge active" }, `${appLabel(hd.platform)}: ${hd.handle}`))));
  if (person.latest_note) {
    const note = person.latest_note.payload || {};
    card.appendChild(h("div", { style: "margin-top:10px;padding-top:10px;border-top:1px dashed var(--border)" },
      h("div", { style: "font-size:10px;color:var(--text-3);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px" },
        `latest note · ${fmtTime(person.latest_note.at)}`),
      h("div", { style: "font-size:12px;color:var(--text-2);white-space:pre-wrap" },
        note.note || paramRows(note).map(([k, v]) => `${k}: ${v}`).join(" · "))));
  }
  return card;
}

async function loadMemory() {
  const box = $("memory-body");
  try {
    state.people = (await api("/api/memory")).people;
    paintMemory();
  } catch (err) {
    box.innerHTML = "";
    box.appendChild(h("div", { class: "card" }, emptyState("Could not load memory & context.", err.message)));
  }
}

function paintMemory() {
  const summary = $("memory-summary");
  const box = $("memory-body");
  if (!summary || !box) return;
  const handleCount = state.people.reduce((n, p) => n + p.handles.length, 0);
  summary.innerHTML = "";
  summary.appendChild(h("span", {}, h("span", { text: `${state.people.length}` }), " recognized people"));
  summary.appendChild(h("span", {}, h("span", { text: `${handleCount}` }), " linked identities"));
  summary.appendChild(h("span", { class: "badge ok" }, "encrypted at rest"));

  box.innerHTML = "";
  if (!state.people.length) {
    box.appendChild(h("div", { class: "card" },
      emptyState("No one recognized yet.", "Aether links identities across platforms as it observes them — they'll appear here.")));
    return;
  }
  const search = $("m-search").value.trim().toLowerCase();
  const visible = state.people.filter((p) => {
    if (!search) return true;
    const note = p.latest_note ? JSON.stringify(p.latest_note.payload) : "";
    return `${p.display_name} ${note}`.toLowerCase().includes(search);
  });
  if (!visible.length) {
    box.appendChild(h("div", { class: "card" }, emptyState("No matching people.", "Try another name or detail from your context graph.")));
    return;
  }
  const grid = h("div", { class: "people-grid" });
  visible.forEach((p) => grid.appendChild(personCard(p)));
  box.appendChild(grid);
}

const RENDERERS = {
  activity: renderActivity, attention: renderAttention,
  tasks: renderTasks, memory: renderMemory,
  audit: renderAudit, traces: renderTraces,
  policy: renderPolicy, apps: renderApps, settings: renderSettings,
};

// --------------------------------------------------------------- router

function currentPageFromHash() {
  const m = /^#\/(\w+)/.exec(location.hash);
  const p = m && RENDERERS[m[1]] ? m[1] : "activity";
  return p;
}

function navigate() {
  const page = currentPageFromHash();
  state.page = page;
  $("page-title").textContent = PAGE_TITLES[page];
  document.title = `Aether — ${PAGE_TITLES[page]}`;
  qsa(".nav-link").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  $("shell").classList.remove("nav-open");
  RENDERERS[page]();
}

// --------------------------------------------------------------- shell chrome

// The inline script in index.html sets documentElement.dataset.theme before
// first paint; these helpers keep the toggle button, the meta theme-color
// and (optionally) the saved preference in sync with it.
function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function applyTheme(t, persist) {
  document.documentElement.dataset.theme = t;
  if (persist) try { localStorage.setItem(THEME_KEY, t); } catch { /* private mode */ }
  const meta = qs('meta[name="theme-color"]');
  if (meta) meta.content = t === "dark" ? "#191c20" : "#f8f7f4";
  const btn = $("theme-btn");
  if (btn) {
    btn.innerHTML = ICONS[t === "dark" ? "sun" : "moon"];
    const label = t === "dark" ? "Switch to light theme" : "Switch to dark theme";
    btn.setAttribute("aria-label", label);
    btn.title = label;
  }
}

function tickClock() {
  const inner = $("topbar-clock")?.querySelector(".t-inner");
  if (!inner) return;
  const now = new Date();
  const day = now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  const pad = (n) => String(n).padStart(2, "0");
  let h24 = now.getHours();
  const ampm = h24 >= 12 ? "pm" : "am";
  const h12 = h24 % 12 || 12;
  inner.innerHTML = "";
  inner.appendChild(h("span", { class: "t-date", text: `${day} · ` }));
  inner.appendChild(h("span", { class: "t", text: `${h12}:${pad(now.getMinutes())}:${pad(now.getSeconds())} ${ampm}` }));
}

function wireShell() {
  mountIcons(document);
  $("topbar-breadcrumb").textContent = `/ ${location.hostname || "localhost"}`;
  tickClock();
  setInterval(tickClock, 1000);
  applyTheme(currentTheme(), false);
  $("theme-btn").addEventListener("click", () => applyTheme(currentTheme() === "dark" ? "light" : "dark", true));
  // follow the system theme only until the user picks one explicitly
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (mq) => {
    let saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch { /* private mode */ }
    if (saved !== "light" && saved !== "dark") applyTheme(mq.matches ? "dark" : "light", false);
  });
  const collapsed = localStorage.getItem(COLLAPSE_KEY) === "1";
  $("shell").classList.toggle("collapsed", collapsed);
  $("collapse-btn").addEventListener("click", () => {
    const now = !$("shell").classList.contains("collapsed");
    $("shell").classList.toggle("collapsed", now);
    localStorage.setItem(COLLAPSE_KEY, now ? "1" : "0");
  });
  const setNav = (open) => {
    $("shell").classList.toggle("nav-open", open);
    $("menu-btn").setAttribute("aria-expanded", String(open));
  };
  $("menu-btn").addEventListener("click", () => setNav(true));
  $("nav-scrim").addEventListener("click", () => setNav(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setNav(false); });
  window.matchMedia("(min-width: 901px)").addEventListener("change", (mq) => { if (mq.matches) setNav(false); });
  window.addEventListener("hashchange", navigate);
}

function boot() {
  wireShell();
  if (!hasToken() && !location.hash) location.hash = "#/settings";
  navigate();
  startPolling();
}

document.addEventListener("DOMContentLoaded", boot);
