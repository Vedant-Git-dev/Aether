// The workspace editor: the five curated files, read and written through
// the token-guarded API. The content is decrypted only on the server and in
// this tab's memory; saving sends the whole file back sealed. Export writes
// a passphrase-protected bundle, import opens one.
import { useEffect, useState } from "react";
import { Button } from "./ui.jsx";
import { SettingsSection } from "./SettingsFrame.jsx";
import { api, token } from "../lib/api.js";
import { useToast } from "./Toasts.jsx";

const FILES = [
  ["identity", "Identity"],
  ["soul", "Soul"],
  ["agents", "How Aether works"],
  ["user", "You"],
  ["memory", "Long-term memory"],
];

export default function SettingsWorkspaceFiles() {
  const toast = useToast();
  const [feed, setFeed] = useState(null);
  const [file, setFile] = useState("identity");
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [status, setStatus] = useState({ text: "", bad: false });
  const [passphrase, setPassphrase] = useState("");
  const [bundle, setBundle] = useState(null);

  const load = () =>
    api("/api/workspace")
      .then((data) => {
        setFeed(data);
        setText(data.files[file]?.raw ?? "");
      })
      .catch((err) => setError(err.message));

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pick = (key) => {
    setFile(key);
    setText(feed?.files?.[key]?.raw ?? "");
    setStatus({ text: "", bad: false });
  };

  const save = async () => {
    setStatus({ text: "Saving…", bad: false });
    try {
      await api(`/api/workspace/${file}`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text }),
      });
      setStatus({ text: "Saved.", bad: false });
      toast("Saved.");
      load();
    } catch (err) {
      setStatus({ text: err.message, bad: true });
    }
  };

  const doExport = async () => {
    if (!passphrase) {
      setStatus({ text: "Enter a passphrase for the export.", bad: true });
      return;
    }
    try {
      const res = await fetch("/api/workspace/export", {
        method: "POST",
        headers: { "x-aether-token": token(), "content-type": "application/json" },
        body: JSON.stringify({ passphrase }),
      });
      if (!res.ok) {
        let detail = `export failed (${res.status})`;
        try {
          detail = (await res.json()).detail || detail;
        } catch {
          /* not json */
        }
        throw new Error(detail);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `aether-workspace-${new Date().toISOString().slice(0, 10)}.aeth`;
      link.click();
      URL.revokeObjectURL(url);
      setStatus({ text: "Exported.", bad: false });
    } catch (err) {
      setStatus({ text: err.message, bad: true });
    }
  };

  const doImport = async () => {
    if (!passphrase || !bundle) {
      setStatus({ text: "Choose an export and enter its passphrase.", bad: true });
      return;
    }
    try {
      const bytes = new Uint8Array(await bundle.arrayBuffer());
      let binary = "";
      for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
      const response = await api("/api/workspace/import", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          passphrase,
          bundle: btoa(binary),
        }),
      });
      setStatus({ text: `Restored ${response.files} file(s).`, bad: false });
      toast("Workspace restored.");
      load();
    } catch (err) {
      setStatus({ text: err.message, bad: true });
    }
  };

  return (
    <SettingsSection
      id="workspace"
      title="Workspace files"
      note="The Markdown files Aether reads on every turn: who it is, how it should behave, what it knows about you, and what it has chosen to keep. Edits here win over generated content. Everything is stored sealed."
      testId="workspace-card"
    >
      {error ? (
        <div className="settings-row py-4 text-[12.5px] text-ink-3">Could not load: {error}</div>
      ) : feed === null ? (
        <div className="settings-row py-4 text-[12.5px] text-ink-3">Loading…</div>
      ) : (
        <>
          <div className="settings-row flex flex-wrap gap-1.5 py-3">
            {FILES.map(([key, label]) => (
              <button
                key={key}
                type="button"
                data-testid={`workspace-file-${key}`}
                onClick={() => pick(key)}
                className={`rounded-full border px-3 py-1 text-[11.5px] ${
                  file === key
                    ? "border-[var(--color-line-strong)] bg-card text-ink"
                    : "border-transparent text-ink-3 hover:text-ink"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="settings-row py-3">
            <textarea
              data-testid="workspace-editor"
              aria-label="Workspace file content"
              value={text}
              onChange={(event) => setText(event.target.value)}
              spellCheck={false}
              className="aether-field min-h-[260px] w-full px-3 py-2 font-mono text-[12.5px] leading-relaxed text-ink"
            />
            <div className="mt-2 flex items-center gap-3">
              <Button kind="approve" data-testid="workspace-save" onClick={save}>
                Save file
              </Button>
              <span className="text-[11.5px] text-ink-3">
                {feed.files[file]?.entries?.length ?? 0} entries
              </span>
            </div>
          </div>
          <div className="settings-row flex flex-wrap items-end gap-2 py-3">
            <label className="flex min-w-[220px] flex-1 flex-col gap-1">
              <span className="text-[11.5px] text-ink-3">Passphrase for export / import</span>
              <input
                type="password"
                data-testid="workspace-passphrase"
                value={passphrase}
                onChange={(event) => setPassphrase(event.target.value)}
                className="aether-field px-3 py-1.5 text-sm text-ink"
              />
            </label>
            <Button kind="neutral" data-testid="workspace-export" onClick={doExport}>
              Export
            </Button>
            <input
              type="file"
              accept=".aeth,application/octet-stream"
              data-testid="workspace-import-file"
              onChange={(event) => setBundle(event.target.files?.[0] ?? null)}
              className="text-[11.5px] text-ink-3"
            />
            <Button kind="neutral" data-testid="workspace-import" onClick={doImport}>
              Import
            </Button>
          </div>
          {status.text ? (
            <div
              data-testid="workspace-status"
              className={`settings-row py-2.5 text-[11.5px] ${
                status.bad ? "text-danger" : "text-ink-3"
              }`}
            >
              {status.text}
            </div>
          ) : null}
        </>
      )}
    </SettingsSection>
  );
}
