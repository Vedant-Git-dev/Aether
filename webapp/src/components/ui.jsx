// Shared flat primitives: bordered cards, one-size controls, static loading
// text. No shadows, no fills, no animations anywhere in this file.
export function Card({ className = "", children, ...rest }) {
  return (
    <div className={`rounded-lg border border-line bg-card ${className}`} {...rest}>
      {children}
    </div>
  );
}

export function SectionLabel({ className = "", children }) {
  return (
    <div className={`text-xs font-medium tracking-wide text-ink-3 uppercase ${className}`}>
      {children}
    </div>
  );
}

// one control style for inputs and selects — neutral, bordered, no fills
export function Field({ className = "", ...rest }) {
  return (
    <input
      className={`rounded-md border border-line-strong bg-card px-3 py-1.5 text-sm text-ink placeholder:text-ink-3 ${className}`}
      {...rest}
    />
  );
}

export function Select({ className = "", children, ...rest }) {
  return (
    <select
      className={`rounded-md border border-line-strong bg-card px-3 py-1.5 text-sm text-ink ${className}`}
      {...rest}
    >
      {children}
    </select>
  );
}

const BTN_KINDS = {
  primary: "border-accent bg-accent text-on-accent hover:opacity-90",
  neutral: "border-line-strong bg-card text-ink hover:bg-subtle",
  approve: "border-success text-success hover:bg-subtle",
  deny: "border-danger text-danger hover:bg-subtle",
};

export function Button({ kind = "neutral", className = "", children, ...rest }) {
  return (
    <button
      className={`rounded-md border px-3.5 py-1.5 text-sm font-medium disabled:cursor-default disabled:opacity-50 ${BTN_KINDS[kind]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  );
}

// the honest loading state: plain text, nothing animated
export function Loading({ label = "Loading…" }) {
  return <div className="px-4 py-6 text-sm text-ink-3">{label}</div>;
}
