// One resolved person: identity, linked handles in plain text, latest note.
import { Card } from "./ui.jsx";
import { fmtTime } from "../lib/format.js";
import { appLabel, humanize, paramRows } from "../lib/describe.js";

export default function PersonCard({ person }) {
  const note = person.latest_note ? person.latest_note.payload || {} : null;

  return (
    <Card className="p-4">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-sm font-semibold text-ink">{person.display_name}</span>
        <span className="shrink-0 text-xs text-ink-3">{Math.round(person.confidence * 100)}% match</span>
      </div>
      <div className="mt-2 text-xs text-ink-2">
        {person.handles.map((hd) => `${appLabel(hd.platform)}: ${hd.handle}`).join(" · ")}
      </div>
      {person.latest_note && (
        <div className="mt-3 border-t border-dashed border-line pt-3">
          <div className="mb-1 text-[10px] font-medium tracking-wide text-ink-3 uppercase">
            Latest note · {fmtTime(person.latest_note.at)}
          </div>
          <div className="text-xs whitespace-pre-wrap text-ink-2">
            {note.note ? (
              <>
                {note.kind && note.kind !== "general" ? (
                  <span className="text-ink-3">{humanize(note.kind)} — </span>
                ) : null}
                {note.note}
              </>
            ) : (
              paramRows(note).map(([k, v]) => `${k}: ${v}`).join(" · ")
            )}
          </div>
        </div>
      )}
    </Card>
  );
}
