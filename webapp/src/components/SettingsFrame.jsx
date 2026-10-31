// The frame around the settings surface: a quiet sticky rail and one row
// language shared by every section. It owns no settings data — the page
// decides what goes in each row. There is deliberately no outer card or
// title bar: Settings is a page, not a floating window.
import { useEffect, useRef } from "react";
import Icon from "./Icon.jsx";

export function SettingsWorkspace({ rail, mobileNav, children }) {
  return (
    <div data-testid="settings-workspace" className="flex w-full flex-col min-[900px]:flex-row">
      {rail}
      <div className="min-w-0 flex-1">
        {mobileNav ? (
          <div
            data-testid="settings-mobile-nav"
            className="aether-chrome sticky top-[52px] z-20 border-b border-[var(--aether-hairline)] px-4 py-2.5 min-[900px]:hidden"
          >
            {mobileNav}
          </div>
        ) : null}
        {children}
      </div>
    </div>
  );
}

export function SettingsSearch({ query, onQuery, testId = "settings-search", className = "" }) {
  const inputRef = useRef(null);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target;
      const tag = target && target.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target?.isContentEditable) {
        return;
      }
      const input = inputRef.current;
      if (!input || input.offsetParent === null) return;
      event.preventDefault();
      input.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <label
      className={`settings-search flex h-9 items-center gap-2 rounded-[8px] px-2.5 ${className}`}
    >
      <Icon name="search" className="h-3.5 w-3.5 shrink-0 text-ink-3" />
      <input
        ref={inputRef}
        data-testid={testId}
        type="text"
        value={query}
        onChange={(event) => onQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            onQuery("");
            event.currentTarget.blur();
          }
        }}
        placeholder="Search settings"
        aria-label="Search settings"
        className="min-w-0 flex-1 bg-transparent text-[12.5px] text-ink outline-none placeholder:text-ink-3"
      />
      {query ? (
        <button
          type="button"
          onClick={() => {
            onQuery("");
            inputRef.current?.focus();
          }}
          aria-label="Clear search"
          className="rounded p-0.5 text-ink-3 hover:text-ink"
        >
          <Icon name="x" className="h-3 w-3" />
        </button>
      ) : (
        <span className="hidden rounded border border-line px-1.5 text-[10px] leading-4 text-ink-3 sm:inline">
          /
        </span>
      )}
    </label>
  );
}

export function SettingsRail({ groups, active, query, onQuery, onSelect }) {
  const hasItems = groups.some((group) => group.items.length > 0);

  return (
    <aside className="settings-rail hidden w-[252px] shrink-0 flex-col border-r border-[var(--aether-hairline)] min-[900px]:sticky min-[900px]:top-[52px] min-[900px]:flex min-[900px]:h-[calc(100dvh-52px)]">
      <div className="px-3 pb-1 pt-3">
        <SettingsSearch query={query} onQuery={onQuery} />
      </div>

      <nav className="settings-scroll min-h-0 flex-1 overflow-y-auto px-2 pb-5 pt-1" aria-label="Settings sections">
        {groups.map((group) => {
          if (group.items.length === 0) return null;
          return (
            <div key={group.label} className="mt-3 first:mt-1">
              <div className="px-2.5 pb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.11em] text-ink-3">
                {group.label}
              </div>
              {group.items.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  data-testid={`settings-nav-${item.key}`}
                  data-active={active === item.key ? "1" : "0"}
                  aria-current={active === item.key ? "true" : undefined}
                  onClick={() => onSelect(item.key)}
                  className="settings-nav-item flex h-8 w-full items-center gap-2.5 rounded-[7px] px-2.5 text-left text-[12.5px] font-medium"
                >
                  <Icon name={item.icon} className="h-4 w-4 shrink-0 opacity-80" />
                  <span className="min-w-0 truncate">{item.title}</span>
                  {item.count ? (
                    <span className="ml-auto pl-1 text-[11px] font-normal tabular-nums text-ink-3">
                      {item.count}
                    </span>
                  ) : null}
                </button>
              ))}
            </div>
          );
        })}

        {!hasItems ? (
          <div className="px-2.5 py-3 text-[12px] leading-relaxed text-ink-3">
            Nothing matches that search.
          </div>
        ) : null}
      </nav>

      <div className="border-t border-[var(--aether-hairline)] px-3 py-3">
        <button
          type="button"
          onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
          className="settings-nav-item flex h-8 w-full items-center gap-2.5 rounded-[7px] px-2.5 text-[12px] font-medium"
        >
          <Icon name="arrow-up" className="h-4 w-4 shrink-0 opacity-80" />
          <span>Back to top</span>
        </button>
      </div>
    </aside>
  );
}

export function SettingsContent({ children }) {
  return (
    <div className="mx-auto w-full px-5 pb-16 pt-5 sm:px-7 lg:px-10">
      {children}
    </div>
  );
}

export function SettingsSection({ id, title, note, badge, testId, children }) {
  return (
    <section
      id={`settings-${id}`}
      data-testid={testId}
      data-settings-section={id}
      aria-labelledby={`settings-${id}-title`}
      className="settings-section scroll-mt-3 py-6 first:pt-4"
    >
      <div className="flex items-baseline justify-between gap-4">
        <h2
          id={`settings-${id}-title`}
          className="text-[15px] font-semibold tracking-[-0.01em] text-ink"
        >
          {title}
        </h2>
        {badge ? <span className="text-[11px] font-medium text-ink-3">{badge}</span> : null}
      </div>
      {note ? (
        <p className="mt-1 max-w-[64ch] text-[12.5px] leading-relaxed text-ink-2">{note}</p>
      ) : null}
      <div className="mt-2">{children}</div>
    </section>
  );
}

export function SettingsRow({
  label,
  hint,
  note,
  error,
  errorTestId,
  hideHint,
  children,
}) {
  return (
    <div className="settings-row grid gap-3 py-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:gap-6">
      <div className="min-w-0">
        <span className="text-[13px] font-medium text-ink">{label}</span>
        {!hideHint && hint ? (
          <p className="mt-1 max-w-[56ch] text-[12px] leading-[1.55] text-ink-3">{hint}</p>
        ) : null}
        {note ? <p className="mt-1 text-[11.5px] text-ink-3">{note}</p> : null}
        {error ? (
          <p data-testid={errorTestId} className="mt-1.5 text-[12px] text-danger">
            {error}
          </p>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center gap-2 sm:justify-end">{children}</div>
    </div>
  );
}

export function QuietInput({ className = "", ...rest }) {
  return (
    <input
      className={`quiet-field w-40 px-2.5 py-1.5 text-center text-[13px] ${className}`}
      {...rest}
    />
  );
}

export function QuietSelect({ className = "", children, ...rest }) {
  return (
    <span className="relative inline-flex items-center">
      <select
        className={`quiet-field quiet-select px-7 py-1.5 text-center text-[13px] ${className}`}
        {...rest}
      >
        {children}
      </select>
      <Icon
        name="chevron"
        className="pointer-events-none absolute right-2 h-3.5 w-3.5 text-ink-3"
      />
    </span>
  );
}

export function Segmented({ label, value, options, onChange }) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          data-active={value === option.value ? "1" : "0"}
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
        >
          {option.icon ? <Icon name={option.icon} className="h-3.5 w-3.5" /> : null}
          <span>{option.label}</span>
        </button>
      ))}
    </div>
  );
}

export function SettingsNotice({ title, children }) {
  return (
    <div className="mt-3 rounded-[8px] border border-[var(--aether-hairline)] bg-subtle/40 px-3.5 py-3">
      {title ? <div className="text-[12.5px] font-medium text-ink">{title}</div> : null}
      <div className="mt-0.5 text-[12px] leading-relaxed text-ink-2">{children}</div>
    </div>
  );
}
