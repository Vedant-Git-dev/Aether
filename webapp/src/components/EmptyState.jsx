// The shared "nothing here" block — a title and one muted line, nothing else.
export default function EmptyState({ title, sub }) {
  return (
    <div className="px-4 py-8 text-center" data-testid="empty-state">
      <div className="text-sm font-medium text-ink-2">{title}</div>
      {sub ? <div className="mt-1 text-xs text-ink-3">{sub}</div> : null}
    </div>
  );
}

// No token yet — the one dead end every page can hit.
export function NoToken() {
  return (
    <div className="px-4 py-8 text-center" data-testid="empty-state">
      <div className="text-sm font-medium text-ink-2">No API token set</div>
      <div className="mt-1 text-xs text-ink-3">
        Add your API token in <a href="#/settings" className="text-accent underline">Settings</a> to connect.
      </div>
    </div>
  );
}

