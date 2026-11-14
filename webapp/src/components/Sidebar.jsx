import Icon from "./Icon.jsx";

// The routes in their original order.
const NAV_ITEMS = [
  ["activity", "Live Activity"],
  ["tasks", "Tasks"],
  ["alerts", "Alerts"],
  ["memory", "Memory & Context"],
  ["audit", "Audit Log"],
  ["traces", "Decision Traces"],
  ["policy", "Authorization & Policies"],
  ["apps", "Apps & Permissions"],
  ["contacts", "Contacts"],
  ["settings", "Settings"],
];

// The mark: a watch dial — an open ring around one lit centre. Flat, single
// colour, no gradient tile. The brass stays free for what is actually waiting.
function Mark({ className }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path
        d="M20.2 8.4A8.4 8.4 0 1 1 15.6 3.8"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
      />
      <circle cx="12" cy="12" r="2.7" fill="currentColor" />
    </svg>
  );
}

export default function Sidebar({ page, collapsed, navOpen, onCollapse, onNavigate }) {
  return (
    <aside
      className={`aether-chrome fixed inset-y-0 left-0 z-40 w-60 flex-col border-r border-[var(--aether-hairline)]
        max-[900px]:shadow-2xl
        min-[901px]:sticky min-[901px]:top-0 min-[901px]:h-screen min-[901px]:flex
        ${collapsed ? "min-[901px]:w-16" : "min-[901px]:w-60"} ${navOpen ? "flex" : "hidden"}`}
      aria-label="Primary"
    >
      <div className={`flex items-center gap-2.5 px-4 ${collapsed ? "min-[901px]:justify-center min-[901px]:px-0" : ""} py-4`}>
        <Mark className="h-6 w-6 shrink-0 text-ink" />
        {!collapsed && (
          <span className="text-[15px] font-semibold text-ink">Aether</span>
        )}
      </div>

      <nav className="settings-scroll flex-1 overflow-y-auto px-2.5 pb-2">
        {NAV_ITEMS.map(([id, label]) => {
          const active = page === id;
          return (
            <a
              key={id}
              href={`#/${id}`}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              title={collapsed ? label : undefined}
              className={`flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-[13px]
                ${collapsed ? "min-[901px]:justify-center min-[901px]:px-0" : ""}
                ${active ? "aether-nav-active font-medium text-ink" : "text-ink-2 hover:bg-subtle hover:text-ink"}`}
            >
              <Icon name={id} className="h-4.5 w-4.5 shrink-0 opacity-75" />
              <span className={`min-w-0 truncate ${collapsed ? "min-[901px]:hidden" : ""}`}>{label}</span>
            </a>
          );
        })}
      </nav>

      <div className="border-t border-[var(--aether-hairline)] p-2.5">
        <button
          type="button"
          onClick={onCollapse}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="hidden h-9 min-[901px]:flex min-[901px]:w-full min-[901px]:items-center rounded-lg px-2.5 text-ink-3 hover:bg-subtle hover:text-ink min-[901px]:justify-center"
        >
          <Icon name={collapsed ? "menu" : "collapse"} className="h-4.5 w-4.5" />
        </button>
      </div>
    </aside>
  );
}
