// The page's one context line. The visible title lives in the topbar, so
// this carries the eyebrow, the plain description and an accessible heading.
export default function PageHead({ eyebrow, title, sub }) {
  return (
    <div className="mb-6 border-b border-[var(--aether-hairline)] pb-4">
      <h2 className="sr-only">{title}</h2>
      <div className="flex items-center gap-2">
        <span className="h-px w-4 bg-accent/70" />
        <span className="text-[10.5px] font-semibold tracking-[0.12em] text-ink-3 uppercase">
          {eyebrow}
        </span>
      </div>
      {sub ? (
        <p className="mt-1.5 max-w-[70ch] text-[13px] leading-relaxed text-ink-2">{sub}</p>
      ) : null}
    </div>
  );
}
