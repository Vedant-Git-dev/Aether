import Icon from "./Icon.jsx";
import Clock from "./Clock.jsx";
import { useTheme } from "../hooks/useTheme.js";

// Menu (mobile only), page title, host breadcrumb, clock, theme toggle,
// attention bell. The bell count is a plain number, nothing more.
export default function Topbar({ title, attentionCount, onMenu }) {
  const { theme, toggle } = useTheme();
  const themeLabel = theme === "dark" ? "Switch to light theme" : "Switch to dark theme";

  return (
    <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-line bg-surface px-4 py-3 min-[901px]:px-6">
      <button
        type="button"
        onClick={onMenu}
        aria-label="Open navigation"
        className="rounded-md border border-line-strong bg-card p-1.5 text-ink-2 hover:bg-subtle max-[900px]:flex min-[901px]:hidden"
      >
        <Icon name="menu" className="h-4 w-4" />
      </button>

      <h1 data-testid="page-title" className="min-w-0 truncate text-base font-semibold text-ink">
        {title}
      </h1>
      <span className="hidden truncate text-xs text-ink-3 min-[701px]:inline">
        / {location.hostname || "localhost"}
      </span>

      <div className="ml-auto flex items-center gap-2.5">
        <Clock />
        <button
          type="button"
          onClick={toggle}
          aria-label={themeLabel}
          title={themeLabel}
          className="rounded-md border border-line-strong bg-card p-1.5 text-ink-2 hover:bg-subtle"
        >
          <Icon name={theme === "dark" ? "sun" : "moon"} className="h-4 w-4" />
        </button>
        <a
          href="#/attention"
          aria-label={attentionCount ? `${attentionCount} awaiting your approval` : "Attention required"}
          className="relative flex items-center rounded-md border border-line-strong bg-card p-1.5 text-ink-2 hover:bg-subtle"
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
