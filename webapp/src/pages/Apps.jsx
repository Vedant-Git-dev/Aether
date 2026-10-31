// What Aether can see and act through: native messaging surfaces, MCP
// servers, direct provider apps, and the chat surfaces the walk can still
// switch on. Everything here reflects live backend state.
import { useCallback, useEffect, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Button, Card, Field, SectionLabel } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import IntegrationCard, { BrandLogo } from "../components/IntegrationCard.jsx";
import GooglePicker from "../components/GooglePicker.jsx";
import { useToast } from "../components/Toasts.jsx";
import { api, hasToken } from "../lib/api.js";
import { assetUrl, ASSET_V } from "../lib/assets.js";

const CONNECTOR_META = {
  telegram: { imgSrc: assetUrl(`icon-telegram.png?v=${ASSET_V}`), subtitle: "Bot API", permission: "Read, reply, monitor" },
  discord: { imgSrc: assetUrl(`icon-discord.png?v=${ASSET_V}`), subtitle: "Gateway", permission: "Read selected servers" },
};

const CATALOG_ICONS = {
  telegram: { imgSrc: assetUrl(`icon-telegram.png?v=${ASSET_V}`) },
  discord: { imgSrc: assetUrl(`icon-discord.png?v=${ASSET_V}`) },
};

// an MCP server's display face — by the name its config entry carries
const SERVER_FACES = {
  gmail: { imgSrc: assetUrl(`icon-gmail.png?v=${ASSET_V}`), label: "Gmail" },
  mail: { imgSrc: assetUrl(`icon-email.jpg?v=${ASSET_V}`), label: "Email" },
  calendar: { imgSrc: assetUrl(`icon-calendar.png?v=${ASSET_V}`), label: "Calendar" },
  github: { imgSrc: assetUrl(`icon-github.png?v=${ASSET_V}`), label: "GitHub" },
  drive: { imgSrc: assetUrl(`icon-drive.png?v=${ASSET_V}`), label: "Google Drive" },
  docs: { imgSrc: assetUrl(`icon-docs.png?v=${ASSET_V}`), label: "Google Docs" },
  linkedin: { imgSrc: assetUrl(`icon-linkedin.png?v=${ASSET_V}`), label: "LinkedIn" },
  notion: { icon: "apps", label: "Notion" },
};

const icon = (p) => assetUrl(`${p}?v=${ASSET_V}`);

const PROVIDER_FACES = {
  gmail: { imgSrc: icon("icon-gmail.png"), label: "Gmail" },
  calendar: { imgSrc: icon("icon-calendar.png"), label: "Calendar" },
  github: { imgSrc: icon("icon-github.png"), label: "GitHub" },
  drive: { imgSrc: icon("icon-drive.png"), label: "Google Drive" },
  docs: { imgSrc: icon("icon-docs.png"), label: "Google Docs" },
  linkedin: { imgSrc: icon("icon-linkedin.png"), label: "LinkedIn" },
};

function ProviderCard({ app, onConnect, onDisconnect, onSaveClient, onSaveToken, onRefresh }) {
  const toast = useToast();
  // setup details stay closed until the user asks for them — a provider
  // that needs setup must not open a form just because the page loaded
  const [setupOpen, setSetupOpen] = useState(false);
  const [tokenOpen, setTokenOpen] = useState(false);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [token, setToken] = useState("");
  const [expiry, setExpiry] = useState("");
  const [busy, setBusy] = useState(false);
  const face = PROVIDER_FACES[app.key] || { icon: "apps", label: app.name };
  const connected = app.status === "active";
  const reauth = app.status === "reauth_required";
  const needsAccess = connected && app.actions === 0;
  const statusText = needsAccess
    ? "Access needed"
    : connected
      ? `Connected${app.identity ? ` — ${app.identity}` : ""} · ${app.actions} action${app.actions === 1 ? "" : "s"}`
      : reauth
        ? `Needs reconnecting${app.last_error ? ` — ${app.last_error}` : ""}`
        : app.setup_required
          ? "Setup needed"
          : "Not connected";
  const statusTone = needsAccess ? "warn" : connected ? "ok" : reauth || app.setup_required ? "warn" : "idle";
  const canSwitchMode = (app.auth_modes || []).includes("oauth") && app.default_auth_mode === "token";
  const alternateMode = app.auth_mode === "oauth" ? "token" : "oauth";
  const alternateLabel = app.auth_mode === "oauth" ? "Use a token" : "Use browser sign-in";

  const connect = async (mode) => {
    setBusy(true);
    try {
      const result = await onConnect(app, mode);
      if (result?.setup_required) setSetupOpen(true);
      if (result?.token_required) setTokenOpen(true);
    } finally {
      setBusy(false);
    }
  };

  const saveClient = async (event) => {
    event.preventDefault();
    if (!clientId.trim() || !clientSecret.trim()) {
      toast("Both the client ID and client secret are needed.", "err");
      return;
    }
    setBusy(true);
    try {
      await onSaveClient(app, clientId, clientSecret);
      setClientId("");
      setClientSecret("");
      setSetupOpen(false);
    } finally {
      setBusy(false);
    }
  };

  const saveToken = async (event) => {
    event.preventDefault();
    if (!token.trim()) {
      toast("Paste the token first.", "err");
      return;
    }
    setBusy(true);
    try {
      await onSaveToken(app, token, expiry);
      setToken("");
      setExpiry("");
      setTokenOpen(false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="flex flex-col gap-3 p-4" data-testid={`provider-${app.key}`}>
      <div className="flex items-center gap-3">
        {face.imgSrc
          ? <img src={face.imgSrc} alt="" className="h-9 w-9 shrink-0 rounded-[7px] object-contain" />
          : <BrandLogo icon={face.icon} />}
        <div className="min-w-0">
          <div role="heading" className="text-sm font-semibold text-ink">{app.name}</div>
          <p className="text-xs text-ink-3">
            {app.auth_mode === "token" ? "Token connection" : "Browser sign-in"}
          </p>
        </div>
        <span className={`ml-auto text-right text-[11.5px] font-medium ${statusTone === "ok" ? "text-success" : statusTone === "warn" ? "text-warning" : "text-ink-3"}`}>
          {statusText}
        </span>
      </div>

      {setupOpen ? (
        <form className="aether-inset flex flex-col gap-2 p-3" onSubmit={saveClient}>
          <div className="text-xs text-ink-2">
            Finish the provider setup once, then connect.
          </div>
          {app.setup_url ? (
            <a className="text-xs text-accent hover:underline" href={app.setup_url} target="_blank" rel="noreferrer">
              Open the provider setup page
            </a>
          ) : null}
          {app.setup_steps?.length ? (
            <ol className="list-decimal space-y-1 pl-4 text-xs text-ink-3">
              {app.setup_steps.map((step, i) => <li key={i}>{step}</li>)}
            </ol>
          ) : null}
          <div className="text-xs text-ink-3">
            Add this redirect URI exactly: <span className="break-all text-ink-2">{app.redirect_uri || "the callback shown by Aether"}</span>
          </div>
          <Field
            type="text"
            autoComplete="off"
            placeholder="Client ID"
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
          />
          <Field
            type="password"
            autoComplete="off"
            placeholder="Client secret"
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
          />
          <div className="flex items-center gap-2">
            <Button type="submit" kind="primary" disabled={busy}>Save client</Button>
            <Button type="button" onClick={() => setSetupOpen(false)} disabled={busy}>Cancel</Button>
          </div>
        </form>
      ) : null}

      {tokenOpen ? (
        <form className="aether-inset flex flex-col gap-2 p-3" onSubmit={saveToken}>
          <div className="text-xs text-ink-2">Paste the token. It is stored encrypted and never shown again.</div>
          {app.token_url ? (
            <a className="text-xs text-accent hover:underline" href={app.token_url} target="_blank" rel="noreferrer">
              Open the token page
            </a>
          ) : null}
          {app.token_steps?.length ? (
            <ol className="list-decimal space-y-1 pl-4 text-xs text-ink-3">
              {app.token_steps.map((step, i) => <li key={i}>{step}</li>)}
            </ol>
          ) : null}
          <Field
            type="password"
            autoComplete="off"
            placeholder={app.token_label || "Token"}
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          {app.token_expires ? (
            <Field
              type="date"
              value={expiry}
              onChange={(e) => setExpiry(e.target.value)}
              aria-label="Token expiry"
            />
          ) : null}
          <div className="flex items-center gap-2">
            <Button type="submit" kind="primary" disabled={busy}>Save token</Button>
            <Button type="button" onClick={() => setTokenOpen(false)} disabled={busy}>Cancel</Button>
          </div>
        </form>
      ) : null}

      {app.key === "drive" && connected && !needsAccess ? (
        <GooglePicker app={app} onSelected={onRefresh} />
      ) : null}
      {app.key === "docs" && connected && !needsAccess ? (
        <div className="text-xs text-ink-3">
          Aether can read and append to the Docs you choose in the Drive card.
        </div>
      ) : null}
      {app.note ? <div className="text-xs text-ink-3">{app.note}</div> : null}

      <div className="flex items-center justify-between gap-2">
        <span className="text-xs text-ink-3">
          {needsAccess
            ? "Add this app's access without disconnecting the others."
            : connected
              ? "What Aether may do is governed by the policy gate."
              : "Connect to let Aether use this app."}
        </span>
        <div className="flex items-center gap-2">
          {canSwitchMode ? (
            <Button type="button" kind="neutral" onClick={() => connect(alternateMode)} disabled={busy}>
              {alternateLabel}
            </Button>
          ) : null}
          {connected && !needsAccess ? (
            <Button type="button" kind="neutral" onClick={() => onDisconnect(app)} disabled={busy}>
              Disconnect
            </Button>
          ) : (
            <Button type="button" kind="primary" onClick={() => connect(app.auth_mode)} disabled={busy}>
              {needsAccess ? "Add access" : reauth ? "Reconnect" : app.setup_required ? "Set up" : "Connect"}
            </Button>
          )}
        </div>
      </div>
    </Card>
  );
}


function AvailableCard({ iconFace, name, blurb }) {
  return (
    <Card className="flex items-center gap-3 p-3.5">
      {iconFace.imgSrc
        ? <img src={iconFace.imgSrc} alt="" className="h-9 w-9 shrink-0 rounded-[7px] object-contain" />
        : <BrandLogo icon={iconFace.icon} />}
      <div className="min-w-0">
        <div className="text-[13px] font-medium text-ink">{name}</div>
        {blurb ? <div className="text-[11.5px] text-ink-3">{blurb}</div> : null}
        <div className="mt-0.5 text-[11px] text-ink-3">Connect with /apps in chat</div>
      </div>
    </Card>
  );
}

export default function AppsPage() {
  const toast = useToast();
  const [data, setData] = useState(null);
  const [providers, setProviders] = useState(undefined); // undefined = not tried, null = not wired
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const d = await api("/api/settings/apps");
      setData(d);
      try { setProviders(await api("/api/providers")); } catch { setProviders(null); }
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => { if (hasToken()) load(); }, [load]);

  const connectProvider = async (app, mode) => {
    try {
      const query = mode ? `?mode=${encodeURIComponent(mode)}` : "";
      const r = await api(`/api/providers/${encodeURIComponent(app.key)}/connect${query}`, { method: "POST" });
      if (r.redirect_url) {
        window.open(r.redirect_url, "_blank", "noopener");
        toast(`Approve ${app.name} in the tab that just opened. Aether will confirm once it's connected.`);
      } else if (r.setup_required) {
        toast(`Finish the ${app.name} setup below, then connect.`);
      } else if (r.token_required) {
        toast(`Paste the ${app.name} token below.`);
      }
      return r;
    } catch (err) {
      toast(err.message, "err");
      return null;
    }
  };

  const disconnectProvider = async (app) => {
    try {
      await api(`/api/providers/${encodeURIComponent(app.key)}/disconnect`, { method: "POST" });
      toast(
        app.auth_mode === "token"
          ? `Disconnected ${app.name}. Remove the saved token at ${app.name} too if you want to revoke it there.`
          : app.key === "linkedin"
            ? `Disconnected ${app.name}. You can also remove Aether in LinkedIn's permitted services.`
            : `Disconnected ${app.name}`,
      );
      load();
    } catch (err) { toast(err.message, "err"); }
  };

  const saveProviderClient = async (app, clientId, clientSecret) => {
    try {
      await api(`/api/providers/${encodeURIComponent(app.provider)}/client`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ client_id: clientId, client_secret: clientSecret }),
      });
      toast(`Saved the ${app.name} client. Now connect it.`);
      load();
    } catch (err) {
      toast(err.message, "err");
      throw err;
    }
  };

  const saveProviderToken = async (app, token, expiry) => {
    try {
      await api(`/api/providers/${encodeURIComponent(app.key)}/token`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ token, expires_at: expiry || null }),
      });
      toast(`Connected ${app.name}`);
      load();
    } catch (err) {
      toast(err.message, "err");
      throw err;
    }
  };

  if (!hasToken()) {
    return (
      <div>
        <PageHead eyebrow="Connections" title="Apps & Permissions"
          sub="What Aether can actually see and act through right now." />
        <Card><NoToken /></Card>
      </div>
    );
  }

  if (error) {
    return (
      <div>
        <PageHead eyebrow="Connections" title="Apps & Permissions"
          sub="What Aether can actually see and act through right now." />
        <Card><EmptyState title="Could not load apps & permissions." sub={error} /></Card>
      </div>
    );
  }

  if (!data) {
    return (
      <div>
        <PageHead eyebrow="Connections" title="Apps & Permissions"
          sub="What Aether can actually see and act through right now." />
        <Card><div className="px-4 py-6 text-sm text-ink-3">Loading…</div></Card>
      </div>
    );
  }

  const cards = [];
  // Connected services only lists surfaces that are actually on. An off
  // surface belongs in Chat surfaces below, never in both places at once.
  data.connectors.filter((c) => c.enabled).forEach((c) => {
    const meta = CONNECTOR_META[c.name] || { icon: "apps", subtitle: "Messaging connector", permission: "Read, reply" };
    cards.push({
      key: `c-${c.name}`,
      logo: <BrandLogo icon={meta.icon} imgSrc={meta.imgSrc} />,
      name: c.name[0].toUpperCase() + c.name.slice(1),
      subtitle: meta.subtitle,
      enabled: true,
      permission: meta.permission,
      statusText: c.has_token ? "On" : "On — no token yet",
      statusTone: c.has_token ? "ok" : "warn",
    });
  });
  // MCP servers with the host's live verdict — up and answering, or not yet
  data.mcp_servers.forEach((s) => {
    const face = SERVER_FACES[s.name.toLowerCase()] || { icon: "apps", label: `MCP: ${s.name}` };
    cards.push({
      key: `s-${s.name}`,
      logo: <BrandLogo icon={face.icon} imgSrc={face.imgSrc} />,
      name: face.label,
      subtitle: `MCP server · ${s.transport}`,
      permission: "Tools exposed by this MCP server",
      enabled: s.enabled,
      statusText: !s.enabled ? "Disabled"
        : s.connected ? `Connected · ${s.actions} action${s.actions === 1 ? "" : "s"}`
        : "Still connecting",
      statusTone: !s.enabled ? "idle" : s.connected ? "ok" : "warn",
    });
  });
  const connectedSurfaces = new Set(data.connectors.filter((c) => c.enabled).map((c) => c.name));
  const addable = (data.catalog || []).filter((r) => !connectedSurfaces.has(r.key));

  return (
    <div>
      <PageHead eyebrow="Connections" title="Apps & Permissions"
        sub="What Aether can actually see and act through right now." />

      <SectionLabel className="mb-2">
        Connected services
        <span className="ml-2 font-normal normal-case text-ink-3">{cards.length} configured</span>
      </SectionLabel>
      <div className="mb-7 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {cards.map((c) => (
          <IntegrationCard key={c.key} data-testid={`integration-${c.key}`} {...c} />
        ))}
      </div>

      <SectionLabel className="mb-2">
        Direct apps
        <span className="ml-2 font-normal normal-case text-ink-3">
          Aether owns the sign-in and the tokens for these apps
        </span>
      </SectionLabel>
      <div className="mb-7">
        {providers === undefined ? (
          <Card className="p-5"><div className="text-[13px] text-ink-3">Loading direct apps…</div></Card>
        ) : providers === null ? (
          <Card className="p-5">
            <div className="text-[13px] text-ink-3">Direct app connections aren't wired on this instance.</div>
          </Card>
        ) : providers.apps.length === 0 ? (
          <Card className="p-5">
            <div className="text-[13px] text-ink-3">
              No direct apps are configured yet. Add a provider definition to make one available here.
            </div>
          </Card>
        ) : (
          <div data-testid="provider-grid" className="grid gap-3 sm:grid-cols-2 2xl:grid-cols-3">
            {providers.apps.map((app) => (
              <ProviderCard
                key={app.key}
                app={app}
                onConnect={connectProvider}
                onDisconnect={disconnectProvider}
                onSaveClient={saveProviderClient}
                onSaveToken={saveProviderToken}
                onRefresh={load}
              />
            ))}
          </div>
        )}
      </div>

      <SectionLabel className="mb-2">
        Chat surfaces
        <span className="ml-2 font-normal normal-case text-ink-3">
          talk to Aether there — run /apps in any chat and the walk takes the tokens there
        </span>
      </SectionLabel>
      {addable.length ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {addable.map((r) => (
            <AvailableCard
              key={r.key}
              iconFace={CATALOG_ICONS[r.key] || { icon: "apps" }}
              name={r.name[0].toUpperCase() + r.name.slice(1)}
              blurb={r.blurb}
            />
          ))}
        </div>
      ) : (
        <Card><EmptyState title="Every chat surface is on." /></Card>
      )}
    </div>
  );
}
