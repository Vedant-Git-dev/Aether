// Local-configuration page. The token lives only in this browser; saving or
// clearing it reloads the page so every poller restarts with the new state.
// Everything else editable renders from the backend's settings feed —
// groups, labels, hints, controls, choices and when-it-bites notes all come
// from /api/settings/fields; this page hardcodes no setting of its own.
import { useEffect, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import ModelPicker from "../components/ModelPicker.jsx";
import { Card, Field, Select, Toggle, Button } from "../components/ui.jsx";
import EmptyState from "../components/EmptyState.jsx";
import { useToast } from "../components/Toasts.jsx";
import { api, token, hasToken } from "../lib/api.js";
import { useBusyAction } from "../lib/busy.js";
import { fmtTime } from "../lib/format.js";
import { friendlyValue, describeProfileSource } from "../lib/describe.js";

export default function SettingsPage() {
  const toast = useToast();
  const [tokenValue, setTokenValue] = useState(() => token());
  const [personality, setPersonality] = useState("");
  const [personalityStatus, setPersonalityStatus] = useState({ text: "", bad: false });
  const [feed, setFeed] = useState(null);
  const [feedError, setFeedError] = useState("");
  const [models, setModels] = useState(null); // the live model catalog for the pickers
  const [overrides, setOverrides] = useState([]);
  const [drafts, setDrafts] = useState({}); // path -> unsaved input text
  const [errors, setErrors] = useState({}); // path -> the backend's plain refusal

  // The model catalog behind every picker — cached server-side by key, so
  // this is one light read per page load. A failed read degrades to a note
  // inside the picker, never a broken page.
  const loadModels = (force = false) =>
    api(`/api/settings/models${force ? "?refresh=1" : ""}`)
      .then(setModels)
      .catch((err) =>
        setModels({ configured: false, providers: [], error: err.message })
      );

  useEffect(() => {
    if (!hasToken()) return;
    api("/api/settings/personality")
      .then((d) => setPersonality(d.text))
      .catch((err) => setPersonalityStatus({ text: `Could not load: ${err.message}`, bad: true }));
    api("/api/settings/fields")
      .then(setFeed)
      .catch((err) => setFeedError(err.message));
    loadModels();
    api("/api/config")
      .then((d) => setOverrides(d.overrides || []))
      .catch(() => setOverrides([])); // the ledger is a nicety, not a gate
  }, []);

  const saveToken = () => {
    const val = tokenValue.trim();
    if (!val) { toast("Enter a token first.", "err"); return; }
    try { localStorage.setItem("aether_token", val); } catch { /* private mode */ }
    location.reload();
  };

  const clearToken = () => {
    try { localStorage.removeItem("aether_token"); } catch { /* private mode */ }
    location.reload();
  };

  const savePersonality = async () => {
    setPersonalityStatus({ text: "Saving…", bad: false });
    try {
      await api("/api/settings/personality", {
        method: "PUT", headers: { "content-type": "application/json" },
        body: JSON.stringify({ text: personality }),
      });
      setPersonalityStatus({ text: "Saved — it takes effect on Aether's next reply.", bad: false });
    } catch (err) {
      setPersonalityStatus({ text: `Not saved: ${err.message}`, bad: true });
    }
  };

  const labelFor = (path) => {
    const field = feed && feed.groups.flatMap((g) => g.fields).find((f) => f.path === path);
    return field ? field.label : path;
  };

  const draftOf = (f) => (f.path in drafts ? drafts[f.path] : f.value === null ? "" : String(f.value));

  const dirty = (f) => draftOf(f) !== (f.value === null ? "" : String(f.value));

  const clearDraft = (f) =>
    setDrafts((dr) => {
      const next = { ...dr };
      delete next[f.path];
      return next;
    });

  const saveField = async (f, value) => {
    setErrors((e) => ({ ...e, [f.path]: "" }));
    try {
      const d = await api(`/api/settings/fields/${f.path}`, {
        method: "PUT", headers: { "content-type": "application/json" },
        body: JSON.stringify({ value }),
      });
      setFeed(d.feed);
      clearDraft(f);
      if (d.reply) toast(d.reply);
    } catch (err) {
      setErrors((e) => ({ ...e, [f.path]: err.message }));
    }
  };

  const resetField = async (f) => {
    setErrors((e) => ({ ...e, [f.path]: "" }));
    try {
      const d = await api(`/api/settings/fields/${f.path}`, { method: "DELETE" });
      setFeed(d.feed);
      clearDraft(f);
      if (d.reply) toast(d.reply);
    } catch (err) {
      setErrors((e) => ({ ...e, [f.path]: err.message }));
    }
  };

  const flipToggle = (f, value) => saveField(f, value);

  const valueToSend = (f) => {
    const d = draftOf(f);
    if (f.kind === "number") return d === "" ? (f.none_ok ? null : "") : Number(d);
    return d === "" && f.none_ok ? null : d;
  };

  const saveCredential = async (cred, value) => {
    try {
      const d = await api(`/api/settings/credentials/${cred.name}`, {
        method: "PUT", headers: { "content-type": "application/json" },
        body: JSON.stringify({ value }),
      });
      setFeed(d);
      // a pasted provider key changes what the model pickers can list —
      // the server's key-keyed cache decides whether to refetch
      loadModels();
      toast(`${cred.label} stored encrypted — it is never shown back.`);
    } catch (err) {
      toast(err.message, "err");
    }
  };

  const control = (f) => {
    if (f.kind === "toggle") {
      return (
        <Toggle
          data-testid={`settings-field-${f.path}`}
          checked={Boolean(f.value)}
          disabled={!hasToken()}
          onChange={(e) => flipToggle(f, e.target.checked)}
        />
      );
    }
    if (f.kind === "model") {
      return (
        <ModelPicker
          field={f}
          draft={draftOf(f)}
          models={models}
          disabled={!hasToken()}
          onDraft={(value) => setDrafts((dr) => ({ ...dr, [f.path]: value }))}
          onRefresh={() => loadModels(true)}
        />
      );
    }
    const shared = {
      "data-testid": `settings-field-${f.path}`,
      disabled: !hasToken(),
      value: draftOf(f),
      onChange: (e) => setDrafts((dr) => ({ ...dr, [f.path]: e.target.value })),
    };
    if (f.kind === "choice") {
      return (
        <Select className="w-44" {...shared}>
          {f.choices.map((c) => <option key={c} value={c}>{c}</option>)}
        </Select>
      );
    }
    return <Field className="w-44" type={f.kind === "number" ? "number" : "text"} {...shared} />;
  };

  return (
    <div className="max-w-3xl">
      <PageHead
        eyebrow="Local configuration"
        title="Settings"
        sub="Configuration Aether actually persists and uses."
      />

      <Card className="mb-4 p-4">
        <h2 className="mb-2 text-sm font-semibold text-ink">API token</h2>
        <p className="mb-3 text-xs text-ink-2">
          Required to reach the chat, approvals, activity, audit and policy endpoints.
          Stored only in this browser's local storage.
        </p>
        <div className="flex gap-2">
          <Field
            type="password"
            placeholder="API token"
            value={tokenValue}
            onChange={(e) => setTokenValue(e.target.value)}
            className="min-w-0 flex-1"
          />
          <Button kind="primary" onClick={saveToken}>Save</Button>
          <Button onClick={clearToken}>Clear</Button>
        </div>
      </Card>

      <Card className="mb-4 p-4">
        <h2 className="mb-2 text-sm font-semibold text-ink">Personality</h2>
        <p className="mb-3 text-xs text-ink-2">
          Free text appended to Aether's real system prompt on every turn — this changes how it
          behaves, not just how it looks. Leave empty for the default behavior.
        </p>
        <textarea
          data-testid="personality-input"
          rows={5}
          disabled={!hasToken()}
          placeholder={hasToken() ? "e.g. Be terse and a little dry. Never use emoji." : "Set your API token above first"}
          value={personality}
          onChange={(e) => setPersonality(e.target.value)}
          className="w-full resize-y rounded-md border border-line-strong bg-card px-3 py-2 font-mono text-sm text-ink placeholder:text-ink-3 disabled:opacity-50"
        />
        <div className="mt-3 flex items-center gap-3">
          <Button kind="primary" data-testid="personality-save" disabled={!hasToken()} onClick={savePersonality}>
            Save personality
          </Button>
          <span
            data-testid="personality-status"
            className={`text-xs ${personalityStatus.bad ? "text-danger" : "text-ink-3"}`}
          >
            {personalityStatus.text}
          </span>
        </div>
      </Card>

      {hasToken() && <ProfileCard />}

      {hasToken() ? (
        feedError ? (
          <EmptyState title="Configuration isn't available." sub={feedError} />
        ) : feed ? (
          <>
            {feed.groups.filter((g) => g.fields.length > 0).map((g) => (
              <Card key={g.key} className="mb-4 p-4">
                <h2 className="mb-1 text-sm font-semibold text-ink">{g.title}</h2>
                {g.note && <p className="mb-3 text-xs text-ink-2">{g.note}</p>}
                {g.fields.map((f) => (
                  <div key={f.path} className="border-t border-line py-3 first:border-t-0 first:pt-0">
                    <div className="flex items-baseline gap-2">
                      <span className="text-sm text-ink">{f.label}</span>
                      {f.changed && (
                        <span className="text-xs text-accent">changed from config.yaml</span>
                      )}
                    </div>
                    <div className="text-xs text-ink-2">{f.hint}</div>
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      {control(f)}
                      {f.kind !== "toggle" && (
                        <Button
                          kind="primary"
                          data-testid={`settings-save-${f.path}`}
                          disabled={!dirty(f) || (draftOf(f) === "" && !f.none_ok)}
                          onClick={() => saveField(f, valueToSend(f))}
                        >
                          Save
                        </Button>
                      )}
                      {f.changed && (
                        <Button
                          data-testid={`settings-reset-${f.path}`}
                          onClick={() => resetField(f)}
                        >
                          Back to config.yaml
                        </Button>
                      )}
                    </div>
                    {f.note && <div className="mt-1 text-xs text-ink-3">{f.note}</div>}
                    {errors[f.path] && (
                      <div data-testid={`settings-error-${f.path}`} className="mt-1 text-xs text-danger">
                        {errors[f.path]}
                      </div>
                    )}
                  </div>
                ))}
              </Card>
            ))}

            <Card className="mb-4 p-4">
              <h2 className="mb-1 text-sm font-semibold text-ink">Credentials</h2>
              <p className="mb-3 text-xs text-ink-2">
                Paste-only: a value goes straight into the encrypted store and is never shown
                back, here or anywhere. What the page knows is only whether one is stored.
              </p>
              {feed.credentials.map((cred) => (
                <div key={cred.name} className="border-t border-line py-3 first:border-t-0 first:pt-0">
                  <div className="flex items-baseline gap-2">
                    <span className="text-sm text-ink">{cred.label}</span>
                    <span
                      data-testid={`settings-cred-set-${cred.name}`}
                      className={`text-xs ${cred.set ? "text-success" : "text-ink-3"}`}
                    >
                      {cred.set ? "stored" : "not stored"}
                    </span>
                  </div>
                  <div className="font-mono text-xs text-ink-3">{cred.name}</div>
                  <div className="text-xs text-ink-2">{cred.hint}</div>
                  <CredentialInput cred={cred} onSave={saveCredential} />
                </div>
              ))}
            </Card>

            {overrides.length > 0 && (
              <Card className="p-4">
                <h2 className="mb-2 text-sm font-semibold text-ink">Changed from config.yaml</h2>
                {overrides.map((o) => (
                  <div key={o.path} className="text-xs text-ink-2">
                    {labelFor(o.path)} = {friendlyValue(o.value)}
                  </div>
                ))}
              </Card>
            )}
          </>
        ) : (
          <div className="py-2 text-sm text-ink-3">Loading…</div>
        )
      ) : (
        <div className="py-2 text-sm text-ink-3">Set your API token above first.</div>
      )}
    </div>
  );
}

// The profile Aether carries into every reply and task: the confirmed
// entries plus the ones it gathered on its own, each waiting for one yes —
// the same flip the chat confirmation makes.
function ProfileCard() {
  const [profile, setProfile] = useState(null);
  const [error, setError] = useState("");
  const [busy, runBusy] = useBusyAction(); // entry id -> a keep in flight
  const [status, setStatus] = useState({ text: "", bad: false });

  const load = () => api("/api/profile").then(setProfile).catch((err) => setError(err.message));
  useEffect(() => { load(); }, []);

  const keep = (id) =>
    runBusy(id, true, async () => {
      try {
        const d = await api("/api/profile/confirm", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ ids: [id] }),
        });
        // zero means it was no longer pending — a stale tap confirms nothing
        setStatus({ text: d.confirmed ? "Noted." : "that one isn't waiting anymore", bad: false });
        load();
      } catch (err) {
        setStatus({ text: err.message, bad: true });
      }
    });

  return (
    <Card className="mb-4 p-4" data-testid="profile-card">
      <h2 className="mb-2 text-sm font-semibold text-ink">What Aether knows about you</h2>
      <p className="mb-3 text-xs text-ink-2">
        The profile Aether carries into every reply and task. Entries it gathered on its own
        wait here for your yes — the same confirmation it asks for in chat.
      </p>
      {error ? (
        <div className="text-sm text-ink-3">Could not load: {error}</div>
      ) : profile === null ? (
        <div className="text-sm text-ink-3">Loading…</div>
      ) : profile.confirmed.length === 0 && profile.pending.length === 0 ? (
        <div className="text-sm text-ink-3">
          Nothing yet — what you tell Aether about yourself will appear here.
        </div>
      ) : (
        <>
          {profile.confirmed.map((e) => (
            <div
              key={e.id}
              data-testid={`profile-confirmed-${e.id}`}
              className="border-t border-line py-2 first:border-t-0 first:pt-0"
            >
              <div className="text-sm text-ink">{e.text}</div>
              <div className="text-xs text-ink-3">
                {describeProfileSource(e.source)} · {fmtTime(e.at)}
              </div>
            </div>
          ))}
          {profile.pending.map((e) => (
            <div key={e.id} data-testid={`profile-pending-${e.id}`} className="border-t border-line py-2">
              <div className="text-sm text-ink">{e.text}</div>
              <div className="text-xs text-ink-3">
                {describeProfileSource(e.source)} · {fmtTime(e.at)}
              </div>
              <div className="mt-2 flex items-center gap-2">
                <Button
                  kind="approve"
                  data-testid={`profile-keep-${e.id}`}
                  disabled={Boolean(busy[e.id])}
                  onClick={() => keep(e.id)}
                >
                  Keep this
                </Button>
              </div>
            </div>
          ))}
          {status.text && (
            <div
              data-testid="profile-status"
              className={`mt-2 text-xs ${status.bad ? "text-danger" : "text-ink-3"}`}
            >
              {status.text}
            </div>
          )}
        </>
      )}
    </Card>
  );
}

// one paste field: the value lives in this component only until Save
// consumes it — it is never rendered back and never kept after
function CredentialInput({ cred, onSave }) {
  const [value, setValue] = useState("");
  const submit = () => {
    const v = value.trim();
    if (!v) return;
    onSave(cred, v);
    setValue("");
  };
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      <Field
        data-testid={`settings-cred-${cred.name}`}
        type="password"
        placeholder="paste a value"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="w-64"
      />
      <Button
        kind="primary"
        data-testid={`settings-cred-save-${cred.name}`}
        disabled={!value.trim()}
        onClick={submit}
      >
        Store
      </Button>
    </div>
  );
}
