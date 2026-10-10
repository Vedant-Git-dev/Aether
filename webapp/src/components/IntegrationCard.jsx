// A connected service: its face, what it exposes, a functional-but-neutral
// switch, and its live status as plain text. The switch and Configure button
// both point at chat — the walk owns the keys, so from here they're signposts.
import Icon from "./Icon.jsx";
import { Card } from "./ui.jsx";
import { useToast } from "./Toasts.jsx";
import { statusColor } from "../lib/status.js";

export const CONFIG_TOAST = "Change this from chat — /apps connects an app, /config changes settings.";

function SlackGlyph() {
  return (
    <span className="grid h-9 w-9 grid-cols-2 place-items-center rounded-md border border-line bg-card p-1.5 text-ink-2" aria-hidden="true">
      {[0, 1, 2, 3].map((i) => <i key={i} className="h-2.5 w-2.5 rounded-[3px] bg-current" />)}
    </span>
  );
}

export function BrandLogo({ icon, iconClass, imgSrc }) {
  if (iconClass === "slack") return <SlackGlyph />;
  if (imgSrc) return <img src={imgSrc} alt="" className="h-9 w-9 rounded-md object-contain" />;
  return (
    <span className="grid h-9 w-9 place-items-center rounded-md border border-line bg-card text-ink-2">
      <Icon name={icon || "apps"} className="h-5 w-5" />
    </span>
  );
}

function Switch({ on, title, onClick }) {
  return (
    <button type="button" role="switch" aria-checked={on} title={title} onClick={onClick}
      className="h-5 w-9 shrink-0 rounded-full border border-line-strong bg-subtle">
      <span className={`block h-3.5 w-3.5 rounded-full ${on ? "ml-[18px] bg-success" : "ml-0.5 bg-ink-3"}`} />
    </button>
  );
}

export default function IntegrationCard({ logo, name, subtitle, enabled, permission, statusText, statusTone }) {
  const toast = useToast();

  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-center gap-3">
        {logo}
        <div className="min-w-0">
          <div role="heading" className="text-sm font-semibold text-ink">{name}</div>
          <p className="text-xs text-ink-3">{subtitle}</p>
        </div>
        <div className="ml-auto">
          <Switch on={enabled} title={`${name} — ${CONFIG_TOAST}`} onClick={() => toast(CONFIG_TOAST)} />
        </div>
      </div>
      <div className="flex items-center gap-1.5 text-xs text-ink-3">
        <Icon name="lock" className="h-3.5 w-3.5" />
        <span>{permission}</span>
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className={`text-xs font-medium ${statusColor(statusTone)}`}>{statusText}</span>
        <button
          type="button"
          onClick={() => toast(CONFIG_TOAST)}
          className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
        >
          Configure <Icon name="arrow" className="h-3 w-3" />
        </button>
      </div>
    </Card>
  );
}
