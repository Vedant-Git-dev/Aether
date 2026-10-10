// Eyebrow + title + one-line description that opens every page.
export default function PageHead({ eyebrow, title, sub }) {
  return (
    <div className="mb-5">
      <div className="text-xs font-medium tracking-wide text-ink-3 uppercase">{eyebrow}</div>
      <div className="mt-1 text-xl font-semibold text-ink">{title}</div>
      {sub ? <div className="mt-1 text-sm text-ink-2">{sub}</div> : null}
    </div>
  );
}
