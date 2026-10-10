// One app from the hub: its logo (a letter tile when the hub has none, or
// the logo URL 404s), the connected identity when it's live, and the
// one-click connect/disconnect. The sign-in itself lives at the hub.
import { useState } from "react";
import { Button } from "./ui.jsx";

export function LetterTile({ name }) {
  return (
    <span data-testid="letter-tile"
      className="grid h-9 w-9 shrink-0 place-items-center rounded-md border border-line bg-card text-sm font-semibold text-ink-2"
      aria-hidden="true">
      {(name || "?").slice(0, 1).toUpperCase()}
    </span>
  );
}

function HubLogo({ toolkit, name }) {
  const [failed, setFailed] = useState(false);
  if (!failed && toolkit && toolkit.logo) {
    return (
      <img
        src={toolkit.logo}
        alt=""
        className="h-9 w-9 shrink-0 rounded-md object-contain"
        onError={() => setFailed(true)}
      />
    );
  }
  return <LetterTile name={name} />;
}

export default function HubCard({ toolkit, account, onConnect, onDisconnect }) {
  const connected = Boolean(account);
  const active = account && account.status === "ACTIVE";

  return (
    <div data-testid="hub-card" className="flex items-center gap-3 rounded-lg border border-line bg-card p-3">
      <HubLogo toolkit={toolkit} name={toolkit.name} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-ink">{toolkit.name}</div>
        <div className="truncate text-xs text-ink-3">
          {connected
            ? (active ? (account.identity || "connected") : `${account.status.toLowerCase()} — reconnect`)
            : (toolkit.description || "connect via the app hub")}
        </div>
      </div>
      {connected ? (
        <Button data-testid="hub-disconnect" onClick={() => onDisconnect(account)}>Disconnect</Button>
      ) : (
        <Button kind="primary" data-testid="hub-connect" onClick={() => onConnect(toolkit)}>Connect</Button>
      )}
    </div>
  );
}
