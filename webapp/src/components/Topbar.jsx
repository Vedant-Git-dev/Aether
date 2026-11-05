import Icon from "./Icon.jsx";
import Clock from "./Clock.jsx";
import { useTheme } from "../hooks/useTheme.js";

// Menu (mobile only), page title, host breadcrumb, clock, theme toggle, and
// the one brass lamp: how many actions sit waiting for a verdict. It points
// at Tasks, where they are decided.
const hostLabel = () => {
  const host = location.hostname || "";
  return !host || host === "localhost" || host === "127.0.0.1" ? "local" : host;
};

export default function Topbar({ title, pendingCount, onMenu }) {
  const { theme, toggle } = useTheme();
  const themeLabel = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";
  const iconBtn =
    "grid h-8 w-8 place-items-center rounded-lg border border-[var(--aether-hairline)] text-ink-2 hover:bg-subtle hover:text-ink";

  return (
    <header className="aether-chrome sticky top-0 z-30 flex items-center gap-3 border-b border-[var(--aether-hairline)] px-[18px] py-2.5 min-[901px]:px-6">
      <button
        type="button"
        onClick={onMenu}
        aria-label="Open navigation"
        className={`${iconBtn} max-[900px]:grid min-[901px]:hidden`}
      >
        <Icon name="menu" className="h-4 w-4" />
      </button>

      <h1 data-testid="page-title" className="min-w-0 truncate text-[15px] font-semibold text-ink">
        {title}
      </h1>
      <span className="hidden truncate text-[11.5px] text-ink-3 min-[701px]:inline">
        {hostLabel()}
      </span>

      <div className="ml-auto flex items-center gap-2">
        <Clock />
        <button
          type="button"
          onClick={toggle}
          aria-label={themeLabel}
          title={themeLabel}
          className={iconBtn}
        >
          <Icon name={theme === "dark" ? "sun" : "moon"} className="h-4 w-4" />
        </button>
        <a
          href="#/tasks"
          aria-label={pendingCount ? `${pendingCount} awaiting your approval` : "Nothing is waiting on you"}
          title={pendingCount ? `${pendingCount} waiting on you` : "Nothing is waiting on you"}
          className={`${iconBtn} relative gap-1.5`}
        >
          <Icon name="bell" className="h-4 w-4" />
          {pendingCount > 0 && (
            <span className="text-[12.5px] font-medium tabular-nums text-accent">{pendingCount}</span>
          )}
        </a>
      </div>
    </header>
  );
}
