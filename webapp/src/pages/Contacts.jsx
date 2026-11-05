// Who may reach Aether: one app at a time. Pick a connected app, search its
// people, add a handle, or open the door for everyone. Cross-app rules show
// in every app's list, tagged, because they count everywhere. The same lists
// chat's /config edits — every change lands through the same validated,
// persisted path and in the audit log.
import { useEffect, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Button, Card, Field, Select } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import { useToast } from "../components/Toasts.jsx";
import { useData } from "../store.jsx";
import { api, hasToken } from "../lib/api.js";
import { personName, platformLabel } from "../lib/describe.js";

// what each app's list governs, in the app's own words
const APP_HINTS = {
  gmail: "While filtering is on, Aether only reads mail from these people — plus mail you sent them, and mail you sent yourself. Everything else is dropped before Aether ever sees it.",
  telegram: "Messages from anyone else are dropped before Aether ever sees them.",
  discord: "Messages from anyone else are dropped before Aether ever sees them.",
};

const appTitle = (key) => platformLabel(key === "gmail" ? "email" : key);

export default function ContactsPage() {
  const toast = useToast();
  const { ensurePeople } = useData();
  const [state, setState] = useState(null); // {mode, allowlist, apps}
  const [error, setError] = useState("");
  const [selectedApp, setSelectedApp] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);

  // the dropdown offers only apps that are actually connected
  const connectedApps = (state?.apps || []).filter((app) => app.connected);
  const app = connectedApps.find((a) => a.key === selectedApp) || connectedApps[0];

  // cross-app rules count on every app, so they appear in every app's list
  // and carry their own Remove path
  const wildcard = (state?.allowlist || []).map((entry) => ({ ...entry, list: "any" }));
  const entries = app ? [...app.allowlist, ...wildcard] : [];
  const needle = query.trim().toLowerCase();
  const visible = entries.filter((entry) => {
    if (!needle) return true;
    return [personName(entry.platform, entry.handle), entry.handle, platformLabel(entry.platform)]
      .some((value) => value.toLowerCase().includes(needle));
  });
  const everyone = app ? (app.mode ?? state.mode) === "off" : false;
  const title = app ? appTitle(app.key) : "";

  useEffect(() => {
    if (!hasToken()) return;
    ensurePeople(); // names win over handles wherever Aether knows the person
    let alive = true;
    api("/api/contacts")
      .then((d) => alive && setState(d))
      .catch((e) => alive && setError(e.message));
    return () => { alive = false; };
  }, []);

  // every write answers with the fresh state, so the page updates in one
  // round-trip — refusals arrive as plain words and only toast
  const write = async (path, opts, okMsg) => {
    setBusy(true);
    try {
      setState(await api(path, opts));
      if (okMsg) toast(okMsg, "ok");
    } catch (err) {
      toast(err.message, "err");
    } finally {
      setBusy(false);
    }
  };

  const jsonOpts = (method, body) => ({
    method,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });

  const add = () => {
    if (!app) return;
    const who = query.trim();
    if (!who) { toast("Type the person's handle first.", "err"); return; }
    write(
      `/api/contacts/apps/${app.key}/allowlist`,
      jsonOpts("POST", { platform: app.key, handle: who }),
      `${personName(app.key, who)} can now reach Aether on ${title}.`,
    );
    setQuery("");
  };

  const removeEntry = (entry) => {
    // an "any" entry lives in the cross-app bucket — removing it takes it
    // off everywhere, not just this app
    const path = entry.list === "any"
      ? "/api/contacts/allowlist"
      : `/api/contacts/apps/${app.key}/allowlist`;
    write(
      path,
      jsonOpts("DELETE", { platform: entry.platform, handle: entry.handle }),
      entry.list === "any"
        ? `${personName(entry.platform, entry.handle)} can no longer reach Aether anywhere.`
        : `${personName(entry.platform, entry.handle)} can no longer reach Aether on ${title}.`,
    );
  };

  const toggleEveryone = () => {
    if (!app) return;
    write(
      `/api/contacts/apps/${app.key}/mode`,
      jsonOpts("PUT", { mode: everyone ? "enforce" : "off" }),
      everyone
        ? `Only the people on the ${title} list can reach Aether now.`
        : `Everyone can reach Aether on ${title} now.`,
    );
  };

  return (
    <div>
      <PageHead
        eyebrow="Privacy"
        title="Contacts"
        sub="Who Aether sees and remembers messages from — one list per app. Pick an app, search its people, or add someone new. The same lists chat's /config edits; changes apply to the next message."
      />
      {!hasToken() ? (
        <Card><NoToken /></Card>
      ) : error ? (
        <Card><EmptyState title="The contact list isn't available." sub={error} /></Card>
      ) : !state ? (
        <div className="py-2 text-sm text-ink-3">Loading…</div>
      ) : !app ? (
        <Card>
          <EmptyState
            title="No app is connected yet."
            sub="Connect an app from Apps, then come back here to choose who can reach Aether on it."
          />
        </Card>
      ) : (
        <Card className="p-5">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <Select
              data-testid="contact-app"
              aria-label="App"
              value={app.key}
              disabled={busy}
              onChange={(e) => { setSelectedApp(e.target.value); setQuery(""); }}
              className="sm:w-44"
            >
              {connectedApps.map((a) => (
                <option key={a.key} value={a.key}>{appTitle(a.key)}</option>
              ))}
            </Select>
            <Field
              data-testid="contact-search"
              type="text"
              aria-label="Search or add a name"
              placeholder="Search or add a name"
              value={query}
              disabled={busy}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") add(); }}
              className="min-w-0 flex-1"
            />
            <div className="flex gap-2">
              <Button
                kind="primary"
                data-testid="contact-add"
                disabled={busy || !query.trim()}
                onClick={add}
              >
                Add
              </Button>
              <Button
                kind={everyone ? "on" : "neutral"}
                data-testid="contact-everyone"
                aria-pressed={everyone}
                disabled={busy}
                onClick={toggleEveryone}
              >
                Everyone
              </Button>
            </div>
          </div>

          <p className="mt-3 text-[12px] text-ink-2" data-testid="contact-mode-text">
            {everyone
              ? `Everyone can reach Aether on ${title} right now.`
              : `Only the people below can reach Aether on ${title}.`}
          </p>
          {APP_HINTS[app.key] ? (
            <p className="mt-1 text-[11.5px] leading-relaxed text-ink-3">{APP_HINTS[app.key]}</p>
          ) : null}

          {visible.length ? (
            <ul className="mt-3 divide-y divide-line border-t border-line">
              {visible.map((entry) => (
                <li
                  key={`${entry.list}/${entry.platform}/${entry.handle}`}
                  data-testid="contact-row"
                  className="flex items-center justify-between gap-3 py-2.5"
                >
                  <div className="min-w-0">
                    <div className="truncate text-[13px] text-ink">
                      {personName(entry.platform, entry.handle)}
                      {entry.list === "any" ? (
                        <span className="ml-2 text-[11.5px] text-ink-3">any app</span>
                      ) : null}
                    </div>
                    <div className="truncate text-[11.5px] text-ink-3">
                      {platformLabel(entry.platform)} · {entry.handle}
                    </div>
                  </div>
                  <Button data-testid="contact-remove" disabled={busy} onClick={() => removeEntry(entry)}>
                    Remove
                  </Button>
                </li>
              ))}
            </ul>
          ) : needle ? (
            <p className="mt-3 border-t border-line pt-3 text-[12.5px] text-ink-3">
              No one on this list matches that search. Add still puts the typed name on it.
            </p>
          ) : (
            <div className="mt-3 border-t border-line pt-3">
              <EmptyState
                title="No one is on this app's list yet."
                sub={everyone
                  ? "Everyone can still reach Aether here; this list is ready for when filtering comes back on."
                  : `While filtering is on, anyone not added here is dropped before Aether ever sees them on ${title}.`}
              />
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
