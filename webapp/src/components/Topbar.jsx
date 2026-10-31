import Icon from "./Icon.jsx";
import Clock from "./Clock.jsx";
import { useTheme } from "../hooks/useTheme.js";

// Menu (mobile only), page title, host breadcrumb, clock, theme toggle,
// attention bell. The bell count is a plain number, nothing more.
export default function Topbar({ title, attentionCount, onMenu }) {
  const { theme, toggle } = useTheme();
  const themeLabel = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";

  return (
    <header className="aether-chrome sticky top-0 z-30 flex items-center gap-3 border-b border-[var(--aether-hairline)] px-4 py-3 min-[901px]:px-6">
      <button
        type="button"
        onClick={onMenu}
        aria-label="Open navigation"
        className="rounded-[7px] border border-[var(--aether-hairline)] bg-card/70 p-1.5 text-ink-2 hover:bg-subtle hover:text-ink max-[900px]:flex min-[901px]:hidden"
      >
        <Icon name="menu" className="h-4 w-4" />
      </button>

      <h1 data-testid="page-title" className="min-w-0 truncate text-[15px] font-semibold tracking-[-0.01em] text-ink">
        {title}
      </h1>
      <span className="hidden truncate text-[11.5px] text-ink-3 min-[701px]:inline">
        / {location.hostname || "localhost"}
      </span>

      <div className="ml-auto flex items-center gap-2.5">
        <Clock />
        <button
          type="button"
          onClick={toggle}
          aria-label={themeLabel}
          title={themeLabel}
          className="rounded-[7px] border border-[var(--aether-hairline)] bg-card/70 p-1.5 text-ink-2 hover:bg-subtle hover:text-ink"
        >
          <Icon name={theme === "dark" ? "sun" : "moon"} className="h-4 w-4" />
        </button>
        <a
          href="#/attention"
          aria-label={attentionCount ? `${attentionCount} awaiting your approval` : "Attention required"}
          className="relative flex items-center rounded-[7px] border border-[var(--aether-hairline)] bg-card/70 p-1.5 text-ink-2 hover:bg-subtle hover:text-ink"
        >
          <Icon name="bell" className="h-4 w-4" />
          {attentionCount > 0 && (
            <span className="ml-1.5 text-sm font-medium text-warning">{attentionCount}</span>
          )}
        </a>
      </div>
    </header>
  );
}
