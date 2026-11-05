// Shared primitives: flat surfaces, one control language, static loading
// text. Depth comes from a hairline and the neutral ramp, not from fills,
// glass or per-card decoration.
export function Card({ className = "", children, ...rest }) {
  return (
    <div className={`aether-card ${className}`} {...rest}>
      {children}
    </div>
  );
}

export function SectionLabel({ className = "", children }) {
  return <div className={`surface-head ${className}`}>{children}</div>;
}

// one control style for inputs and selects — a plain field on a hairline,
// text against the left edge where the eye already is
export function Field({ className = "", ...rest }) {
  return (
    <input
      className={`aether-field px-3 py-1.5 text-[13px] text-ink placeholder:text-ink-3 ${className}`}
      {...rest}
    />
  );
}

export function Select({ className = "", children, ...rest }) {
  return (
    <select
      className={`aether-field px-3 py-1.5 text-[13px] text-ink ${className}`}
      {...rest}
    >
      {children}
    </select>
  );
}

// The on/off control. The input keeps the accessible name and any test
// hook; the visual switch itself is the quiet pill in index.css.
export function Toggle({ className = "", label, ...rest }) {
  return (
    <label className={`inline-flex items-center gap-2.5 text-sm text-ink-2 ${className}`}>
      <input type="checkbox" className="aether-switch" {...rest} />
      {label && <span>{label}</span>}
    </label>
  );
}

// primary is the ramp turned solid (ink on light, off-white on dark) so the
// one accent stays free for meaning; approve/deny are semantic text on a
// hairline, never a coloured tile.
const BTN_KINDS = {
  primary: "border-ink bg-ink text-surface hover:opacity-88",
  neutral: "border-[var(--color-line-strong)] bg-card text-ink hover:bg-subtle",
  on: "border-accent/45 bg-accent/8 text-accent hover:bg-accent/14",
  approve: "border-success/35 text-success hover:bg-success/8",
  deny: "border-danger/35 text-danger hover:bg-danger/8",
};

export function Button({ kind = "neutral", className = "", children, ...rest }) {
  return (
    <button
      className={`inline-flex items-center justify-center whitespace-nowrap rounded-lg border px-3.5 py-1.5 text-[13px] font-medium transition-colors active:opacity-80 disabled:cursor-default disabled:opacity-50 ${BTN_KINDS[kind]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}

// the honest loading state: one plain line in the same quiet voice as an
// empty state, never a spinner or a skeleton
export function Loading({ label = "Loading…" }) {
  return (
    <div className="px-4 py-10 text-center text-[13px] text-ink-3" data-testid="loading-state">
      {label}
    </div>
  );
}
