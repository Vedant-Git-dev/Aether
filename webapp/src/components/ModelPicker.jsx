// The model dropdown: one field's picker over the live model catalog.
//
// Everything it renders comes from the two backend feeds — the field's
// own spec (its sentinel choices, plain-worded by the backend) and the
// reachable slice of the model catalog (grouped per provider; a provider
// that is not up is left out, never shown as a dead group). A saved value
// the catalog doesn't know is kept, shown as selected, and said so
// plainly — never silently reset. No free-text entry anywhere: every
// choice is a value the backend named.
import { useEffect, useRef, useState } from "react";
import Icon from "./Icon.jsx";
import { Button, Field } from "./ui.jsx";

// how a stored value reads in the picker: the sentinel words map to their
// plain labels ("base" is the legacy spelling of "default"), anything
// else is the value itself. sentinels[0] is the field's own word for
// what an empty value behaves as, so an unset field shows its meaning
// instead of a blank.
const optionFor = (value, sentinels) => {
  const first = sentinels[0];
  if (value === null || value === "")
    return first ? { value: first.value, label: first.label } : { value: "", label: "" };
  if (value === "base" || value === "default") {
    const match = sentinels.find((s) => s.value === "default");
    if (match) return { value: match.value, label: match.label };
  }
  const sentinel = sentinels.find((s) => s.value === value);
  return sentinel ? { ...sentinel } : { value: String(value), label: String(value) };
};

export default function ModelPicker({
  field,
  draft,
  models,
  onDraft,
  onRefresh,
  disabled,
}) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const boxRef = useRef(null);

  const sentinels = field.sentinels || [];
  const selected = optionFor(draft, sentinels);

  // Every reachable provider's models, in the provider-prefixed display
  // form — also the membership test for "is the saved value still known".
  // Providers that are not up are left out entirely: their name and an
  // error line are not a setting a person can choose from.
  const providers = (models?.providers || []).filter(
    (group) => !group.error && group.models.length > 0
  );
  const allModels = providers.flatMap((g) =>
    g.models.map((id) => ({ full: `${g.provider}/${id}` }))
  );
  const knownFull = new Set(allModels.map((m) => m.full));
  const isSentinel = sentinels.some((s) => s.value === selected.value);
  // a bare id — typed before the dropdown existed — is still an available
  // model when a provider serves it; only what no key reaches is "not in
  // the available models", and a sentinel never is. NVIDIA vendor ids carry
  // their own slash ("meta/llama-…"), so a slash is not disqualifying: a
  // prefixed value is already covered by knownFull above, and a bare one
  // matches the provider's raw id here.
  const known =
    knownFull.has(selected.value) ||
    (!isSentinel &&
      providers.some((g) => g.models.includes(selected.value)));
  const hasList = allModels.length > 0;
  const unknown = selected.value && !isSentinel && !known;

  useEffect(() => {
    if (!open) return;
    const close = (e) => {
      if (boxRef.current && !boxRef.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const choose = (value) => {
    onDraft(value);
    setOpen(false);
    setFilter("");
  };

  // the filter forgives spelling the way people say it: "gemini 3.1 flash"
  // finds "gemini/gemini-3.1-flash-lite". Two passes — the whole query
  // with every non-alphanumeric dropped (seams and punctuation vanish),
  // then every word of it appearing somewhere in the label's own words.
  const norm = (s) => s.toLowerCase().replace(/[^a-z0-9]/g, "");
  const words = (s) => s.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  const needle = filter.trim();
  const wanted = words(needle);
  const matches = (label) => {
    if (!needle) return true;
    if (norm(label).includes(norm(needle))) return true;
    const have = words(label);
    return wanted.length > 0 && wanted.every((w) => have.includes(w));
  };
  const sentinelHits = sentinels.filter((s) => matches(s.label));
  const modelHits = allModels.filter((m) => matches(m.full));

  return (
    <div className="relative flex w-full flex-col items-end gap-1 sm:w-auto" ref={boxRef}>
      <button
        type="button"
        data-testid={`settings-field-${field.path}`}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        className="quiet-field relative flex w-full items-center justify-center px-2.5 py-1.5 text-center text-[13px] text-ink disabled:opacity-50 sm:w-[232px]"
      >
        <span className="min-w-0 max-w-full truncate px-5">
          {selected.label || <span className="text-ink-3">pick a model</span>}
        </span>
        <Icon name="chevron" className="absolute right-2 h-3.5 w-3.5 shrink-0 text-ink-3" />
      </button>
      {unknown && hasList && (
        <span
          data-testid={`settings-model-unknown-${field.path}`}
          className="max-w-full text-center text-[11px] leading-snug text-ink-3 sm:max-w-[232px]"
        >
          not in the available models — kept as saved
        </span>
      )}

      {open && (
        <div
          data-testid={`settings-model-list-${field.path}`}
          className="aether-popover absolute left-0 right-auto top-full z-10 mt-1 max-h-72 w-full max-w-[calc(100vw-2rem)] overflow-y-auto p-2 sm:left-auto sm:right-0 sm:w-80"
        >
          <Field
            data-testid={`settings-model-search-${field.path}`}
            type="text"
            placeholder="filter models"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setOpen(false);
                setFilter("");
              }
            }}
            autoFocus
            className="mb-2 w-full"
          />

          {!models ? (
            <div className="px-1 py-2 text-sm text-ink-3">Loading the available models…</div>
          ) : models.error ? (
            <div className="px-1 py-2 text-sm text-ink-3">
              The model list couldn't be loaded — {models.error}
            </div>
          ) : !models.configured ? (
            <div className="px-1 py-2 text-sm text-ink-3">
              No provider key is set yet — paste one under Credentials below and this list
              fills with the models it reaches.
            </div>
          ) : (
            <>
              {sentinelHits.map((s) => (
                <button
                  type="button"
                  key={s.value}
                  data-testid={`settings-model-option-${s.value}`}
                  onClick={() => choose(s.value)}
                  className={`block w-full rounded px-2 py-1 text-left text-sm hover:bg-subtle ${
                    selected.value === s.value ? "text-accent" : "text-ink"
                  }`}
                >
                  {s.label}
                </button>
              ))}
              {unknown && matches(selected.value) && (
                <button
                  type="button"
                  data-testid={`settings-model-option-unknown-${field.path}`}
                  onClick={() => choose(selected.value)}
                  className="block w-full truncate rounded px-2 py-1 text-left font-mono text-sm text-ink hover:bg-subtle"
                >
                  {selected.value}
                  <span className="font-sans text-ink-3"> — not in the available models</span>
                </button>
              )}
              {providers.map((g) => (
                <div key={g.provider} className="mt-1">
                  <div className="px-2 py-1 text-xs font-medium tracking-wide text-ink-3 uppercase">
                    {g.provider}
                  </div>
                  {g.models
                    .map((id) => `${g.provider}/${id}`)
                    .filter((full) => matches(full))
                    .map((full) => (
                      <button
                        type="button"
                        key={full}
                        data-testid={`settings-model-option-${full}`}
                        onClick={() => choose(full)}
                        className={`block w-full truncate rounded px-2 py-1 text-left font-mono text-sm hover:bg-subtle ${
                          selected.value === full ? "text-accent" : "text-ink"
                        }`}
                      >
                        {full}
                      </button>
                    ))}
                </div>
              ))}
              {providers.length === 0 && sentinelHits.length === 0 && !unknown && (
                <div className="px-2 py-1 text-sm text-ink-3">
                  No provider could be reached right now. Refresh the list when one is back.
                </div>
              )}
              {providers.length > 0 &&
                modelHits.length === 0 &&
                sentinelHits.length === 0 &&
                !unknown && (
                  <div className="px-2 py-1 text-sm text-ink-3">Nothing matches that filter.</div>
                )}
            </>
          )}

          <div className="mt-2 border-t border-line pt-2">
            <Button
              data-testid={`settings-models-refresh-${field.path}`}
              onClick={onRefresh}
              className="text-xs"
            >
              Refresh the list
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
