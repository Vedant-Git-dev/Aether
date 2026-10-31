// The shared "nothing here" block — one quiet line and, when useful, one
// muted sentence. It sits inside a card, so it adds no border of its own.
export default function EmptyState({ title, sub }) {
  return (
    <div className="px-4 py-10 text-center" data-testid="empty-state">
      <div className="text-[13px] font-medium text-ink-2">{title}</div>
      {sub ? (
        <div className="mx-auto mt-1.5 max-w-[52ch] text-[12px] leading-relaxed text-ink-3">
          {sub}
        </div>
      ) : null}
    </div>
  );
}

// No token yet — the one dead end every page can hit.
export function NoToken() {
  return (
    <div className="px-4 py-10 text-center" data-testid="empty-state">
      <div className="text-[13px] font-medium text-ink-2">No API token set</div>
      <div className="mx-auto mt-1.5 max-w-[52ch] text-[12px] leading-relaxed text-ink-3">
        Add your API token in <a href="#/settings" className="text-accent underline">Settings</a> to connect.
      </div>
    </div>
  );
}

