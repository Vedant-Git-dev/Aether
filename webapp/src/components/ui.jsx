// Shared primitives: quiet glass surfaces, one control language, static
// loading text. The depth comes from the shell and CSS tokens, not from
// per-card decoration.
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

// one control style for inputs and selects — glass fill, defined hairline
export function Field({ className = "", ...rest }) {
  return (
    <input
      className={`aether-field px-3 py-1.5 text-center text-sm text-ink placeholder:text-ink-3 ${className}`}
      {...rest}
    />
  );
}

export function Select({ className = "", children, ...rest }) {
  return (
    <select
      className={`aether-field aether-select px-3 py-1.5 text-sm text-ink ${className}`}
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

const BTN_KINDS = {
  primary: "border-accent bg-accent text-on-accent hover:opacity-90",
  neutral: "border-[var(--color-line-strong)] bg-card text-ink hover:bg-subtle hover:text-ink",
  approve: "border-success/55 bg-success/5 text-success hover:bg-success/10",
  deny: "border-danger/55 bg-danger/5 text-danger hover:bg-danger/10",
};

export function Button({ kind = "neutral", className = "", children, ...rest }) {
  return (
    <button
      className={`inline-flex items-center justify-center whitespace-nowrap rounded-[7px] border px-3.5 py-1.5 text-sm font-medium shadow-[var(--aether-raise)] transition-colors active:opacity-80 disabled:cursor-default disabled:opacity-50 ${BTN_KINDS[kind]} ${className}`}
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
