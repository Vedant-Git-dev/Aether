// The profile Aether carries into every reply and task: the confirmed
// entries plus the ones it gathered on its own, each waiting for one yes —
// the same flip the chat confirmation makes.
import { useEffect, useState } from "react";
import { Button } from "./ui.jsx";
import { SettingsSection } from "./SettingsFrame.jsx";
import { api } from "../lib/api.js";
import { useBusyAction } from "../lib/busy.js";
import { fmtTime } from "../lib/format.js";
import { describeProfileSource } from "../lib/describe.js";

export default function SettingsProfile() {
  const [profile, setProfile] = useState(null);
  const [error, setError] = useState("");
  const [busy, runBusy] = useBusyAction(); // entry id -> a keep in flight
  const [status, setStatus] = useState({ text: "", bad: false });

  const load = () => api("/api/profile").then(setProfile).catch((err) => setError(err.message));
  useEffect(() => {
    load();
  }, []);

  const keep = (id) =>
    runBusy(id, true, async () => {
      try {
        const d = await api("/api/profile/confirm", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ ids: [id] }),
        });
        // zero means it was no longer pending — a stale tap confirms nothing
        setStatus({ text: d.confirmed ? "Noted." : "that one isn't waiting anymore", bad: false });
        load();
      } catch (err) {
        setStatus({ text: err.message, bad: true });
      }
    });

  const pending = profile?.pending?.length || 0;

  return (
    <SettingsSection
      id="profile"
      title="What Aether knows about you"
      note="The profile Aether carries into every reply and task. Entries it gathered on its own wait here for your yes — the same confirmation it asks for in chat."
      badge={pending ? `${pending} waiting` : null}
      testId="profile-card"
    >
      {error ? (
        <div className="settings-row py-4 text-[12.5px] text-ink-3">Could not load: {error}</div>
      ) : profile === null ? (
        <div className="settings-row py-4 text-[12.5px] text-ink-3">Loading…</div>
      ) : profile.confirmed.length === 0 && profile.pending.length === 0 ? (
        <div className="settings-row py-4 text-[12.5px] text-ink-3">
          Nothing yet — what you tell Aether about yourself will appear here.
        </div>
      ) : (
        <>
          {profile.confirmed.map((entry) => (
            <div
              key={entry.id}
              data-testid={`profile-confirmed-${entry.id}`}
              className="settings-row flex flex-wrap items-center gap-4 py-3"
            >
              <div className="min-w-0 flex-1">
                <div className="text-[13px] text-ink">{entry.text}</div>
                <div className="mt-0.5 text-[11.5px] text-ink-3">
                  {describeProfileSource(entry.source)} · {fmtTime(entry.at)}
                </div>
              </div>
            </div>
          ))}
          {profile.pending.map((entry) => (
            <div
              key={entry.id}
              data-testid={`profile-pending-${entry.id}`}
              className="settings-row flex flex-wrap items-center gap-4 py-3"
            >
              <div className="min-w-0 flex-1">
                <div className="text-[13px] text-ink">{entry.text}</div>
                <div className="mt-0.5 text-[11.5px] text-ink-3">
                  {describeProfileSource(entry.source)} · {fmtTime(entry.at)}
                </div>
              </div>
              <Button
                kind="approve"
                data-testid={`profile-keep-${entry.id}`}
                disabled={Boolean(busy[entry.id])}
                onClick={() => keep(entry.id)}
                className="text-[12px]"
              >
                Keep this
              </Button>
            </div>
          ))}
          {status.text ? (
            <div
              data-testid="profile-status"
              className={`settings-row py-2.5 text-[11.5px] ${status.bad ? "text-danger" : "text-ink-3"}`}
            >
              {status.text}
            </div>
          ) : null}
        </>
      )}
    </SettingsSection>
  );
}
