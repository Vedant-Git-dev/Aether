// The Drive file picker. Google's Picker grants Aether's OAuth client
// per-file access with the drive.file scope; the browser gets its own
// short-lived Google token, so the backend never hands a provider token
// to the page. The selected ids are verified server-side before they are
// remembered.
import { useEffect, useState } from "react";
import { Button, Field } from "./ui.jsx";
import { useToast } from "./Toasts.jsx";
import { api } from "../lib/api.js";

let googleScripts = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[src="${src}"]`);
    if (existing?.dataset.loaded === "true") {
      resolve();
      return;
    }
    const script = existing || document.createElement("script");
    script.src = src;
    script.async = true;
    script.defer = true;
    script.onload = () => {
      script.dataset.loaded = "true";
      resolve();
    };
    script.onerror = () => reject(new Error("Google's picker scripts could not load."));
    if (!existing) document.head.appendChild(script);
  });
}

function loadGoogle() {
  if (googleScripts) return googleScripts;
  googleScripts = Promise.all([
    loadScript("https://apis.google.com/js/api.js"),
    loadScript("https://accounts.google.com/gsi/client"),
  ])
    .then(
      () =>
        new Promise((resolve, reject) => {
          if (!window.gapi) {
            reject(new Error("Google's picker library is unavailable."));
            return;
          }
          window.gapi.load("picker", () => resolve());
        }),
    )
    .catch((err) => {
      googleScripts = null;
      throw err;
    });
  return googleScripts;
}

export default function GooglePicker({ app, onSelected }) {
  const toast = useToast();
  const [recipe, setRecipe] = useState(undefined);
  const [setupOpen, setSetupOpen] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [appId, setAppId] = useState("");
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState([]);

  useEffect(() => {
    let alive = true;
    api(`/api/providers/${encodeURIComponent(app.provider)}/picker`)
      .then((value) => {
        if (alive) setRecipe(value);
      })
      .catch(() => {
        if (alive) setRecipe(null);
      });
    return () => {
      alive = false;
    };
  }, [app.provider]);

  const saveSetup = async (event) => {
    event.preventDefault();
    if (!apiKey.trim() || !appId.trim()) {
      toast("The API key and project number are both needed.", "err");
      return;
    }
    setBusy(true);
    try {
      const value = await api(`/api/providers/${encodeURIComponent(app.provider)}/picker`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ api_key: apiKey.trim(), app_id: appId.trim() }),
      });
      setRecipe(value);
      setApiKey("");
      setAppId("");
      setSetupOpen(false);
      toast("Saved the Google Picker setup.");
    } catch (err) {
      toast(err.message, "err");
    } finally {
      setBusy(false);
    }
  };

  const recordSelection = async (files) => {
    try {
      const result = await api(`/api/providers/${encodeURIComponent(app.key)}/picked`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ files }),
      });
      setSelected(result.selected || []);
      toast(
        result.selected?.length === 1
          ? "Aether can now read and append to that document."
          : `Aether can now read and append to ${result.selected?.length || 0} documents.`,
      );
      onSelected?.();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      setBusy(false);
    }
  };

  const showPicker = (token) => {
    const pickerLib = window.google?.picker;
    if (!pickerLib) {
      toast("Google's picker library is unavailable.", "err");
      setBusy(false);
      return;
    }
    const view = new pickerLib.DocsView(pickerLib.ViewId.DOCUMENTS);
    view.setMode(pickerLib.DocsViewMode.LIST);
    const picker = new pickerLib.PickerBuilder()
      .addView(view)
      .setOAuthToken(token)
      .setDeveloperKey(recipe.api_key)
      .setAppId(recipe.app_id)
      .setOrigin(window.location.origin)
      .setCallback((data) => {
        if (data[pickerLib.Response.ACTION] !== pickerLib.Action.PICKED) {
          setBusy(false);
          return;
        }
        const files = (data[pickerLib.Response.DOCUMENTS] || []).map((doc) => ({
          id: doc[pickerLib.Document.ID],
          name: doc[pickerLib.Document.NAME],
          mime_type: doc[pickerLib.Document.MIME_TYPE],
        }));
        recordSelection(files);
      })
      .build();
    picker.setVisible(true);
  };

  const chooseFiles = async () => {
    if (!recipe?.configured) {
      setSetupOpen(true);
      return;
    }
    setBusy(true);
    try {
      await loadGoogle();
      const tokenClient = window.google.accounts.oauth2.initTokenClient({
        client_id: recipe.client_id,
        scope: recipe.scope,
        callback: (response) => {
          if (response.error) {
            toast("Google didn't finish the file-picker sign-in.", "err");
            setBusy(false);
            return;
          }
          showPicker(response.access_token);
        },
      });
      tokenClient.requestAccessToken({ prompt: "" });
    } catch (err) {
      toast(err.message || "Google's file picker could not open.", "err");
      setBusy(false);
    }
  };

  if (recipe === undefined) return null;
  if (recipe === null) {
    return (
      <div className="text-xs text-ink-3">
        The Google file picker isn't available on this instance.
      </div>
    );
  }

  return (
    <div className="aether-inset p-3" data-testid="drive-picker">
      <div className="text-xs text-ink-2">
        Choose the Google Docs Aether may read and append to. Access is limited to
        the files you pick.
      </div>
      {selected.length ? (
        <div className="mt-1 text-xs text-ink-3">
          Selected: {selected.map((file) => file.name || "untitled document").join(", ")}
        </div>
      ) : null}
      {setupOpen || !recipe.configured ? (
        <form className="mt-2 flex flex-col gap-2" onSubmit={saveSetup}>
          <div className="text-xs text-ink-3">
            In Google Cloud, enable the Google Picker API, create an API key, and
            allow this page's origin. Add the same origin to the OAuth client's
            authorized JavaScript origins. For the API key's website restriction,
            add <span className="break-all text-ink-2">{window.location.origin}</span>{" "}
            and <span className="break-all text-ink-2">https://docs.google.com/*</span>.
          </div>
          {recipe.setup_url ? (
            <a
              className="text-xs text-accent hover:underline"
              href={recipe.setup_url}
              target="_blank"
              rel="noreferrer"
            >
              Open the Google credentials page
            </a>
          ) : null}
          <Field
            type="text"
            autoComplete="off"
            placeholder="Google API key"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
          <Field
            type="text"
            autoComplete="off"
            placeholder="Google Cloud project number"
            value={appId}
            onChange={(e) => setAppId(e.target.value)}
          />
          <div className="flex items-center gap-2">
            <Button type="submit" kind="primary" disabled={busy}>
              Save picker setup
            </Button>
            {recipe.configured ? (
              <Button type="button" onClick={() => setSetupOpen(false)} disabled={busy}>
                Cancel
              </Button>
            ) : null}
          </div>
        </form>
      ) : (
        <div className="mt-2 flex items-center gap-2">
          <Button type="button" kind="primary" onClick={chooseFiles} disabled={busy}>
            Choose files
          </Button>
          <Button type="button" kind="neutral" onClick={() => setSetupOpen(true)} disabled={busy}>
            Picker setup
          </Button>
        </div>
      )}
    </div>
  );
}
