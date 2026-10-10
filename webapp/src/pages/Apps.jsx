// What Aether can see and act through: native messaging surfaces, MCP
// servers, the Composio app hub (searchable), and the chat surfaces the
// walk can still switch on. Everything here reflects live backend state.
import { useCallback, useEffect, useState } from "react";
import PageHead from "../components/PageHead.jsx";
import { Card, Field, SectionLabel } from "../components/ui.jsx";
import EmptyState, { NoToken } from "../components/EmptyState.jsx";
import IntegrationCard, { BrandLogo, CONFIG_TOAST } from "../components/IntegrationCard.jsx";
import HubCard from "../components/HubCard.jsx";
import { useToast } from "../components/Toasts.jsx";
import { api, hasToken } from "../lib/api.js";
import { assetUrl, ASSET_V } from "../lib/assets.js";

// module-level on purpose: the search keeps its text across page switches
let hubQuery = "";

const CONNECTOR_META = {
  telegram: { imgSrc: assetUrl(`icon-telegram.png?v=${ASSET_V}`), subtitle: "Bot API", permission: "Read, reply, monitor" },
  discord: { imgSrc: assetUrl(`icon-discord.png?v=${ASSET_V}`), subtitle: "Gateway", permission: "Read selected servers" },
  slack: { iconClass: "slack", subtitle: "Socket mode", permission: "Read, search, draft" },
};

const CATALOG_ICONS = {
  telegram: { imgSrc: assetUrl(`icon-telegram.png?v=${ASSET_V}`) },
  discord: { imgSrc: assetUrl(`icon-discord.png?v=${ASSET_V}`) },
  slack: { iconClass: "slack" },
};

// an MCP server's display face — by the name its config entry carries
const SERVER_FACES = {
  gmail: { imgSrc: assetUrl(`icon-gmail.png?v=${ASSET_V}`), label: "Gmail" },
  mail: { imgSrc: assetUrl(`icon-email.jpg?v=${ASSET_V}`), label: "Email" },
  calendar: { imgSrc: assetUrl(`icon-calendar.png?v=${ASSET_V}`), label: "Calendar" },
  github: { imgSrc: assetUrl(`icon-github.png?v=${ASSET_V}`), label: "GitHub" },
  notion: { icon: "apps", label: "Notion" },
};

const icon = (p) => assetUrl(`${p}?v=${ASSET_V}`);

function AvailableCard({ iconFace, name, blurb }) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-line bg-card p-3">
      {iconFace.imgSrc
        ? <img src={iconFace.imgSrc} alt="" className="h-9 w-9 shrink-0 rounded-md object-contain" />
        : <BrandLogo icon={iconFace.icon} iconClass={iconFace.iconClass} />}
      <div className="min-w-0">
        <div className="text-sm font-medium text-ink">{name}</div>
        {blurb ? <div className="text-xs text-ink-3">{blurb}</div> : null}
        <div className="mt-0.5 text-xs text-ink-3">Connect with /apps in chat</div>
      </div>
    </div>
  );
}

export default function AppsPage() {
  const toast = useToast();
  const [data, setData] = useState(null);
  const [hub, setHub] = useState(undefined); // undefined = not tried, null = not wired
  const [error, setError] = useState("");
  const [query, setQuery] = useState(hubQuery);

  const load = useCallback(async () => {
    try {
      const d = await api("/api/settings/apps");
      setData(d);
      try { setHub(await api("/api/connections")); } catch { setHub(null); }
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => { if (hasToken()) load(); }, [load]);

  const connectHubApp = async (t) => {
    try {
      const r = await api(`/api/connections/${encodeURIComponent(t.slug)}/connect`, { method: "POST" });
      window.open(r.redirect_url, "_blank", "noopener");
      toast(`Approve ${t.name} in the tab that just opened. Aether will confirm in chat once it's connected.`);
    } catch (err) { toast(err.message, "err"); }
  };

  const disconnectHubApp = async (account) => {
    try {
      await api(`/api/connections/${encodeURIComponent(account.id)}/disconnect`, { method: "POST" });
      toast(`Disconnected ${account.toolkit}`);
      load();
    } catch (err) { toast(err.message, "err"); }
  };

  const onSearch = (e) => { hubQuery = e.target.value; setQuery(e.target.value); };

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
  // native messaging surfaces — "on" honestly means the keys are in place
  data.connectors.forEach((c) => {
    const meta = CONNECTOR_META[c.name] || { icon: "apps", subtitle: "Messaging connector", permission: "Read, reply" };
    const missingKeys = c.enabled && !c.has_token;
    cards.push({
      key: `c-${c.name}`,
      logo: <BrandLogo icon={meta.icon} iconClass={meta.iconClass} imgSrc={meta.imgSrc} />,
      name: c.name[0].toUpperCase() + c.name.slice(1),
      subtitle: meta.subtitle,
      enabled: c.enabled,
      permission: meta.permission,
      statusText: !c.enabled ? "Disabled" : missingKeys ? "On — no token yet" : "On",
      statusTone: !c.enabled ? "idle" : missingKeys ? "warn" : "ok",
    });
  });
  // MCP servers with the host's live verdict — up and answering, or not yet
  data.mcp_servers.forEach((s) => {
    const face = SERVER_FACES[s.name.toLowerCase()] || { icon: "apps", label: `MCP: ${s.name}` };
    cards.push({
      key: `s-${s.name}`,
      logo: <BrandLogo icon={face.icon} iconClass={face.iconClass} imgSrc={face.imgSrc} />,
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
  cards.push({
    key: "screen-vision",
    logo: <BrandLogo imgSrc={icon("icon-phone.png")} />,
    name: "Screen vision",
    subtitle: "OCR, perception only",
    permission: "Screenshots become memory, never actions",
    enabled: data.screen_vision_available,
    statusText: data.screen_vision_available ? "Available" : "Unavailable",
    statusTone: data.screen_vision_available ? "ok" : "idle",
  });
  cards.push({
    key: "contacts",
    logo: <BrandLogo imgSrc={icon("icon-monitor.png")} />,
    name: "Contact allowlist",
    subtitle: "Ingestion filter",
    permission: "Filters senders before they reach memory",
    enabled: data.contacts_mode === "enforce",
    statusText: data.contacts_mode,
    statusTone: data.contacts_mode === "enforce" ? "ok" : "idle",
  });

  // the hub grid: matching toolkits first, connected ones first among them,
  // and connected accounts whose toolkit left the catalog still render —
  // otherwise they could never be disconnected from this page
  const q = query.trim().toLowerCase();
  const bySlug = hub ? new Map(hub.connected.map((a) => [a.toolkit, a])) : new Map();
  const shown = hub
    ? hub.toolkits
        .filter((t) => !q || t.name.toLowerCase().includes(q) || t.slug.toLowerCase().includes(q))
        .sort((a, b) => Number(bySlug.has(b.slug)) - Number(bySlug.has(a.slug)))
    : [];
  const orphans = hub
    ? hub.connected.filter((a) =>
        !hub.toolkits.some((t) => t.slug === a.toolkit)
        && (!q || a.toolkit.toLowerCase().includes(q)))
    : [];

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
      <div className="mb-6 grid gap-3 sm:grid-cols-2">
        {cards.map((c) => <IntegrationCard key={c.key} {...c} />)}
      </div>

      <SectionLabel className="mb-2">
        App hub
        <span className="ml-2 font-normal normal-case text-ink-3">
          every app Composio can connect — the sign-in lives at the hub, never here
        </span>
      </SectionLabel>
      <div className="mb-6">
        {!hub ? (
          <Card className="p-4">
            <div className="text-sm text-ink-3">The app hub isn't wired on this instance.</div>
          </Card>
        ) : !hub.configured ? (
          <Card className="p-4">
            <div className="text-sm text-ink-3">
              No hub key yet — run /apps add {'<app>'} in chat and provide the Composio API key once.
              Every app after that is one click.
            </div>
          </Card>
        ) : (
          <>
            <Field
              data-testid="hub-search"
              type="search"
              placeholder="Search apps"
              value={query}
              onChange={onSearch}
              className="mb-3"
            />
            {shown.length === 0 && orphans.length === 0 ? (
              <Card>
                <EmptyState
                  title="No apps match."
                  sub={q ? `Nothing in the hub matches "${q}".` : "The hub has no apps to offer."}
                />
              </Card>
            ) : (
              <div data-testid="hub-grid" className="grid gap-3 sm:grid-cols-2">
                {orphans.map((a) => (
                  <HubCard key={`o-${a.id}`} toolkit={{ slug: a.toolkit, name: a.toolkit, logo: "" }}
                    account={a} onConnect={connectHubApp} onDisconnect={disconnectHubApp} />
                ))}
                {shown.map((t) => (
                  <HubCard key={t.slug} toolkit={t} account={bySlug.get(t.slug)}
                    onConnect={connectHubApp} onDisconnect={disconnectHubApp} />
                ))}
              </div>
            )}
            <div className="mt-3 text-xs text-ink-3">
              Connected means the hub holds the sign-in. What Aether may do with it is governed by the
              policy gate — reads run automatically; sends and deletes require your approval.
            </div>
          </>
        )}
      </div>

      <SectionLabel className="mb-2">
        Chat surfaces
        <span className="ml-2 font-normal normal-case text-ink-3">
          talk to Aether there — run /apps in any chat and the walk takes the tokens there
        </span>
      </SectionLabel>
      {addable.length ? (
        <div className="grid gap-3 sm:grid-cols-2">
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
