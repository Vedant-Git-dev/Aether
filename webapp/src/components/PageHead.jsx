// The page's one context line. The visible title lives in the topbar, so
// this carries the eyebrow, the plain description and an accessible heading.
export default function PageHead({ eyebrow, title, sub }) {
  return (
    <div className="mb-5 border-b border-[var(--aether-hairline)] pb-4">
      <h2 className="sr-only">{title}</h2>
      {eyebrow ? <div className="text-[12px] font-medium text-ink-3">{eyebrow}</div> : null}
      {sub ? (
        <p className="mt-1 max-w-[76ch] text-[13px] leading-relaxed text-ink-2">{sub}</p>
      ) : null}
    </div>
  );
}
