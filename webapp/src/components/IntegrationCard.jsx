// A connected service: its face, what it exposes, a functional switch, and
// its live status as plain text. The switch and Configure button both point
// at chat — the walk owns the keys, so from here they're signposts.
import Icon from "./Icon.jsx";
import { Card } from "./ui.jsx";
import { useToast } from "./Toasts.jsx";
import { statusColor } from "../lib/status.js";

export const CONFIG_TOAST = "Change this from chat — /apps connects an app, /config changes settings.";

export function BrandLogo({ icon, imgSrc }) {
  if (imgSrc) return <img src={imgSrc} alt="" className="h-9 w-9 rounded-[7px] object-contain" />;
  return (
    <span className="grid h-9 w-9 place-items-center rounded-[7px] border border-[var(--aether-hairline)] bg-card text-ink-2 shadow-[var(--aether-raise)]">
      <Icon name={icon || "apps"} className="h-5 w-5" />
    </span>
  );
}

function Switch({ on, title, onClick }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      title={title}
      onClick={onClick}
      className="aether-switch aether-switch-button shrink-0"
    />
  );
}

export default function IntegrationCard({ logo, name, subtitle, enabled, permission, statusText, statusTone, ...rest }) {
  const toast = useToast();

  return (
    <Card className="flex flex-col gap-3 p-4" {...rest}>
      <div className="flex items-center gap-3">
        {logo}
        <div className="min-w-0">
          <div role="heading" className="text-[13px] font-semibold text-ink">{name}</div>
          <p className="text-[11.5px] text-ink-3">{subtitle}</p>
        </div>
        <div className="ml-auto">
          <Switch on={enabled} title={`${name} — ${CONFIG_TOAST}`} onClick={() => toast(CONFIG_TOAST)} />
        </div>
      </div>
      <div className="flex items-center gap-1.5 text-[11.5px] text-ink-3">
        <Icon name="lock" className="h-3.5 w-3.5" />
        <span>{permission}</span>
      </div>
      <div className="mt-auto flex items-center justify-between gap-2">
        <span className={`text-[11.5px] font-medium ${statusColor(statusTone)}`}>{statusText}</span>
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
