// Local configuration, shaped like one quiet page: a searchable rail,
// grouped sections, and one label-left/control-right row language.
// Everything editable still renders from the backend's settings feed —
// groups, labels, hints, controls, choices and when-it-bites notes all come
// from /api/settings/fields; this page hardcodes no setting of its own.
import { useEffect, useMemo, useState } from "react";
import Icon from "../components/Icon.jsx";
import ModelPicker from "../components/ModelPicker.jsx";
import CredentialInput from "../components/SettingsCredential.jsx";
import SettingsProfile from "../components/SettingsProfile.jsx";
import { Button, Toggle } from "../components/ui.jsx";
import {
  QuietInput,
  QuietSelect,
  Segmented,
  SettingsContent,
  SettingsNotice,
  SettingsRail,
  SettingsRow,
  SettingsSearch,
  SettingsSection,
  SettingsWorkspace,
} from "../components/SettingsFrame.jsx";
import { useToast } from "../components/Toasts.jsx";
import { useTheme } from "../hooks/useTheme.js";
import { api, token, hasToken } from "../lib/api.js";
import {
  CHOICE_LABELS,
  GROUP_ORDER,
  HINTS_KEY,
  SECTION_META,
  cleanGroup,
  credentialTitle,
  faceFor,
  humanise,
  normalise,
  readHints,
} from "../lib/settings.js";

function ServiceMark({ face }) {
  if (face.imgSrc) {
    return <img src={face.imgSrc} alt="" className="h-7 w-7 shrink-0 rounded-[7px] object-contain" />;
  }
  return (
    <span className="aether-inset grid h-7 w-7 shrink-0 place-items-center rounded-[7px] text-ink-2">
      <Icon name={face.icon || "server"} className="h-4 w-4" />
    </span>
  );
}

export default function SettingsPage() {
  const toast = useToast();
  const authed = hasToken();
  const { preference, setPreference } = useTheme();
  const [tokenValue, setTokenValue] = useState(() => token());
  const [personality, setPersonality] = useState("");
  const [personalityLoaded, setPersonalityLoaded] = useState(false);
  const [personalityStatus, setPersonalityStatus] = useState({ text: "", bad: false });
  const [feed, setFeed] = useState(null);
  const [feedError, setFeedError] = useState("");
  const [models, setModels] = useState(null); // the live model catalog for the pickers
  const [drafts, setDrafts] = useState({}); // path -> unsaved input text
  const [errors, setErrors] = useState({}); // path -> the backend's plain refusal
  const [query, setQuery] = useState("");
  const [active, setActive] = useState("general");
  const [jumpTo, setJumpTo] = useState("");
  const [showHints, setShowHints] = useState(readHints);

  // The model catalog behind every picker — cached server-side by key, so
  // this is one light read per page load. A failed read degrades to a note
  // inside the picker, never a broken page.
  const loadModels = (force = false) =>
    api(`/api/settings/models${force ? "?refresh=1" : ""}`)
      .then(setModels)
      .catch((err) =>
        setModels({ configured: false, providers: [], error: err.message })
      );

  useEffect(() => {
    if (!authed) return;
    api("/api/settings/personality")
      .then((d) => {
        setPersonality(d.text);
        setPersonalityLoaded(true);
      })
      .catch((err) => {
        setPersonalityStatus({ text: `Could not load: ${err.message}`, bad: true });
        setPersonalityLoaded(true);
      });
    api("/api/settings/fields")
      .then(setFeed)
      .catch((err) => setFeedError(err.message));
    loadModels();
  }, [authed]);

  const saveToken = () => {
    const val = tokenValue.trim();
    if (!val) {
      toast("Enter a token first.", "err");
      return;
    }
    try {
      localStorage.setItem("aether_token", val);
    } catch {
      /* private mode */
    }
    location.reload();
  };

  const clearToken = () => {
    try {
      localStorage.removeItem("aether_token");
    } catch {
      /* private mode */
    }
    location.reload();
  };

  const savePersonality = async () => {
    setPersonalityStatus({ text: "Saving…", bad: false });
    try {
      await api("/api/settings/personality", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text: personality }),
      });
      setPersonalityStatus({ text: "Saved — it takes effect on Aether's next reply.", bad: false });
    } catch (err) {
      setPersonalityStatus({ text: `Not saved: ${err.message}`, bad: true });
    }
  };

  const draftOf = (f) => (f.path in drafts ? drafts[f.path] : f.value === null ? "" : String(f.value));
  const dirty = (f) => draftOf(f) !== (f.value === null ? "" : String(f.value));

  const clearDraft = (f) =>
    setDrafts((dr) => {
      const next = { ...dr };
      delete next[f.path];
      return next;
    });

  const saveField = async (f, value) => {
    setErrors((e) => ({ ...e, [f.path]: "" }));
    try {
      const d = await api(`/api/settings/fields/${f.path}`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ value }),
      });
      setFeed(d.feed);
      clearDraft(f);
      if (d.reply) toast(d.reply);
    } catch (err) {
      setErrors((e) => ({ ...e, [f.path]: err.message }));
    }
  };

  const flipToggle = (f, value) => saveField(f, value);

  const valueToSend = (f) => {
    const d = draftOf(f);
    if (f.kind === "number") return d === "" ? (f.none_ok ? null : "") : Number(d);
    return d === "" && f.none_ok ? null : d;
  };

  const saveCredential = async (cred, value) => {
    try {
      const d = await api(`/api/settings/credentials/${cred.name}`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ value }),
      });
      setFeed(d);
      // a pasted provider key changes what the model pickers can list —
      // the server's key-keyed cache decides whether to refetch
      loadModels();
      toast(`${cred.label} stored encrypted — it is never shown back.`);
    } catch (err) {
      toast(err.message, "err");
    }
  };

  const control = (f) => {
    if (f.kind === "toggle") {
      return (
        <Toggle
          data-testid={`settings-field-${f.path}`}
          checked={Boolean(f.value)}
          disabled={!authed}
          aria-label={f.label}
          onChange={(e) => flipToggle(f, e.target.checked)}
        />
      );
    }
    if (f.kind === "model") {
      return (
        <ModelPicker
          field={f}
          draft={draftOf(f)}
          models={models}
          disabled={!authed}
          onDraft={(value) => setDrafts((dr) => ({ ...dr, [f.path]: value }))}
          onRefresh={() => loadModels(true)}
        />
      );
    }
    const shared = {
      "data-testid": `settings-field-${f.path}`,
      disabled: !authed,
      value: draftOf(f),
      onChange: (e) => setDrafts((dr) => ({ ...dr, [f.path]: e.target.value })),
    };
    if (f.kind === "choice") {
      return (
        <QuietSelect className="w-40" {...shared}>
          {f.choices.map((c) => (
            <option key={c} value={c}>
              {CHOICE_LABELS[c] || humanise(c)}
            </option>
          ))}
        </QuietSelect>
      );
    }
    return (
      <QuietInput
        className={f.kind === "number" ? "w-24 tabular-nums" : "w-56"}
        type={f.kind === "number" ? "number" : "text"}
        {...shared}
      />
    );
  };

  const groupsByKey = useMemo(() => {
    const map = new Map();
    (feed?.groups || []).forEach((group) => map.set(group.key, cleanGroup(group)));
    return map;
  }, [feed]);

  const credentials = feed?.credentials || [];
  const q = normalise(query).trim();
  const hit = (...values) => !q || values.some((value) => normalise(value).includes(q));

  const feedViews = useMemo(() => {
    const views = {};
    SECTION_META.forEach((section) => {
      if (!section.feed) return;
      const group = groupsByKey.get(section.key);
      if (!group) {
        views[section.key] = null;
        return;
      }
      const titleHit = Boolean(q) && hit(group.title, group.note);
      const fields =
        !q || titleHit
          ? group.fields
          : group.fields.filter(
              (field) =>
                normalise(field.label).includes(q) ||
                normalise(field.hint).includes(q) ||
                normalise(field.note).includes(q) ||
                normalise(field.path).includes(q)
            );
      views[section.key] = {
        group,
        fields,
        visible: !q || titleHit || fields.length > 0,
      };
    });
    return views;
  }, [groupsByKey, q]);

  const credentialsAvailable = authed && Boolean(feed);
  const credentialsTitleHit = hit("Credentials", "Keys", "Secrets", "Paste-only");
  const visibleCredentials =
    !q || credentialsTitleHit
      ? credentials
      : credentials.filter(
          (cred) =>
            normalise(cred.label).includes(q) ||
            normalise(cred.hint).includes(q) ||
            normalise(cred.name).includes(q) ||
            normalise(credentialTitle(cred)).includes(q)
        );
  const credentialsVisible =
    credentialsAvailable && (!q || credentialsTitleHit || visibleCredentials.length > 0);

  const sections = SECTION_META.map((section) => {
    if (section.key === "general") {
      return {
        ...section,
        available: true,
        visible: hit("General", "Appearance", "Theme", "Light", "Dark", "System", "Descriptions", "Hints"),
      };
    }
    if (section.feed) {
      const view = feedViews[section.key] || null;
      return { ...section, available: Boolean(view), visible: Boolean(view?.visible) };
    }
    if (section.key === "credentials") {
      return { ...section, available: credentialsAvailable, visible: credentialsVisible };
    }
    if (section.key === "profile") {
      return {
        ...section,
        available: authed,
        visible: authed && hit("What Aether knows", "Profile", "Memory", "Facts", "About you"),
      };
    }
    if (section.key === "personality") {
      return {
        ...section,
        available: authed,
        visible: authed && hit("Personality", "Voice", "Tone", "System prompt", "Style"),
      };
    }
    if (section.key === "access") {
      return {
        ...section,
        available: true,
        visible: hit("Access", "API token", "Token", "Security", "Privacy"),
      };
    }
    return { ...section, available: true, visible: true };
  });

  const visibleSections = sections.filter((section) => section.visible);
  const availableSections = sections.filter((section) => section.available);
  const railGroups = GROUP_ORDER.map((label) => ({
    label,
    items: sections
      .filter((section) => section.group === label && section.visible)
      .map(({ key, title, icon }) => ({ key, title, icon })),
  }));

  const goSection = (key) => {
    setQuery("");
    setActive(key);
    setJumpTo(key);
  };

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "auto" });
  }, [q]);

  useEffect(() => {
    if (!jumpTo) return;
    const element = document.getElementById(`settings-${jumpTo}`);
    if (element) {
      const mobileNav = document.querySelector('[data-testid="settings-mobile-nav"]');
      const offset =
        window.innerWidth >= 900 ? 64 : (mobileNav?.getBoundingClientRect().bottom ?? 148) + 12;
      const top = element.getBoundingClientRect().top + window.scrollY - offset;
      window.scrollTo({ top, behavior: "auto" });
    }
    setJumpTo("");
  }, [jumpTo, q]);

  const visibleKeySignature = visibleSections.map((section) => section.key).join("|");
  useEffect(() => {
    const onScroll = () => {
      const mobileNav = document.querySelector('[data-testid="settings-mobile-nav"]');
      const line =
        window.innerWidth >= 900 ? 84 : (mobileNav?.getBoundingClientRect().bottom ?? 120) + 32;
      let current = visibleSections[0]?.key || "general";
      visibleSections.forEach((section) => {
        const element = document.getElementById(`settings-${section.key}`);
        if (element && element.getBoundingClientRect().top <= line) current = section.key;
      });
      const atBottom =
        window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
      if (atBottom) current = visibleSections[visibleSections.length - 1]?.key || current;
      setActive((previous) => (previous === current ? previous : current));
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [visibleKeySignature]);

  const toggleHints = (checked) => {
    setShowHints(checked);
    try {
      localStorage.setItem(HINTS_KEY, checked ? "1" : "0");
    } catch {
      /* private mode */
    }
  };

  const fieldRow = (f) => (
    <SettingsRow
      key={f.path}
      label={f.label}
      hint={f.hint}
      note={f.note}
      hideHint={!showHints}
      error={errors[f.path]}
      errorTestId={`settings-error-${f.path}`}
    >
      {f.kind !== "toggle" && dirty(f) ? (
        <button
          type="button"
          data-testid={`settings-save-${f.path}`}
          disabled={draftOf(f) === "" && !f.none_ok}
          onClick={() => saveField(f, valueToSend(f))}
          className="rounded-[6px] px-2 py-1 text-[12px] font-medium text-accent hover:bg-accent/10 disabled:cursor-default disabled:opacity-40"
        >
          Save
        </button>
      ) : null}
      {control(f)}
    </SettingsRow>
  );

  const surfaceTable = (view, kind) => {
    const fields = view.fields;
    if (fields.length === 0) return null;
    const onCount = fields.filter((field) => Boolean(field.value)).length;
    return (
      <>
        <div className="surface-head mt-1 grid grid-cols-[minmax(0,1fr)_84px] gap-3 px-1 pb-1.5 sm:grid-cols-[minmax(0,1fr)_112px_112px]">
          <span>Service</span>
          <span className="hidden sm:block">Type</span>
          <span className="text-right">Status</span>
        </div>
        {fields.map((f) => {
          const face = faceFor(f.path);
          return (
            <div
              key={f.path}
              className="surface-row grid grid-cols-[minmax(0,1fr)_84px] items-center gap-3 px-1 py-2.5 sm:grid-cols-[minmax(0,1fr)_112px_112px]"
            >
              <div className="flex min-w-0 items-center gap-2.5">
                <ServiceMark face={face} />
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-[13px] font-medium text-ink">
                      {face.label || humanise(f.label)}
                    </span>
                  </div>
                  {showHints ? (
                    <div className="truncate text-[11.5px] text-ink-3" title={f.hint}>
                      {f.hint}
                    </div>
                  ) : null}
                  {errors[f.path] ? (
                    <div data-testid={`settings-error-${f.path}`} className="mt-0.5 text-[11.5px] text-danger">
                      {errors[f.path]}
                    </div>
                  ) : null}
                </div>
              </div>
              <div className="hidden text-[12px] text-ink-3 sm:block">{kind}</div>
              <div className="flex items-center justify-end gap-2.5">
                <span className={`text-[11.5px] font-medium ${f.value ? "text-success" : "text-ink-3"}`}>
                  {f.value ? "On" : "Off"}
                </span>
                <Toggle
                  data-testid={`settings-field-${f.path}`}
                  checked={Boolean(f.value)}
                  disabled={!authed}
                  aria-label={f.label}
                  onChange={(e) => flipToggle(f, e.target.checked)}
                />
              </div>
            </div>
          );
        })}
        <div className="surface-row flex items-center justify-end py-2 text-[11px] text-ink-3">
          {onCount} of {fields.length} on
        </div>
      </>
    );
  };

  const renderSection = (section) => {
    if (section.key === "general") {
      return (
        <SettingsSection
          key="general"
          id="general"
          title="General"
          note="Appearance and reading comfort on this device."
        >
          <SettingsRow
            label="Theme"
            hint="Follow this device, or keep one appearance across Aether."
            hideHint={!showHints}
          >
            <div data-testid="settings-theme">
              <Segmented
                label="Theme"
                value={preference}
                onChange={setPreference}
                options={[
                  { value: "system", label: "System", icon: "monitor" },
                  { value: "light", label: "Light", icon: "sun" },
                  { value: "dark", label: "Dark", icon: "moon" },
                ]}
              />
            </div>
          </SettingsRow>
          <SettingsRow
            label="Descriptions"
            hint="Show the sentence under each setting that explains what it does."
            hideHint={!showHints}
          >
            <Toggle
              data-testid="settings-hints"
              checked={showHints}
              aria-label="Show setting descriptions"
              onChange={(e) => toggleHints(e.target.checked)}
            />
          </SettingsRow>
          {!authed ? (
            <div className="pb-1 pt-3">
              <SettingsNotice title="A token unlocks the rest">
                Aether needs the API token for this instance before it can read or change
                configuration. The token stays in this browser.
                <button
                  type="button"
                  onClick={() => goSection("access")}
                  className="ml-2 font-medium text-accent hover:underline"
                >
                  Go to Access
                </button>
              </SettingsNotice>
            </div>
          ) : null}
        </SettingsSection>
      );
    }

    if (section.key === "credentials") {
      const stored = credentials.filter((cred) => cred.set).length;
      return (
        <SettingsSection
          key="credentials"
          id="credentials"
          title="Credentials"
          note="Paste-only: a value goes straight into the encrypted store and is never shown back, here or anywhere. This page only knows whether one is stored."
          badge={credentials.length ? `${stored} stored` : null}
        >
          {visibleCredentials.map((cred) => (
            <SettingsRow
              key={cred.name}
              label={credentialTitle(cred)}
              hint={cred.hint}
              hideHint={!showHints}
            >
              <div className="flex flex-col items-end gap-1.5">
                <span
                  data-testid={`settings-cred-set-${cred.name}`}
                  className={`text-[11.5px] font-medium ${cred.set ? "text-success" : "text-ink-3"}`}
                >
                  {cred.set ? "stored" : "not stored"}
                </span>
                <CredentialInput cred={cred} onSave={saveCredential} />
              </div>
            </SettingsRow>
          ))}
        </SettingsSection>
      );
    }

    if (section.key === "profile") return <SettingsProfile key="profile" />;

    if (section.key === "personality") {
      return (
        <SettingsSection
          key="personality"
          id="personality"
          title="Personality"
          note="Free text appended to Aether's real system prompt on every turn — this changes how it behaves, not just how it looks. Leave empty for the default behavior."
        >
          {!personalityLoaded ? (
            <div className="py-4 text-[12.5px] text-ink-3">Reading the current personality…</div>
          ) : (
            <div className="py-4">
              <textarea
                data-testid="personality-input"
                rows={5}
                disabled={!authed}
                placeholder={
                  authed
                    ? "e.g. Be terse and a little dry. Never use emoji."
                    : "Set your API token under Access first"
                }
                value={personality}
                onChange={(e) => setPersonality(e.target.value)}
                className="quiet-field quiet-field-edge min-h-[132px] w-full resize-y px-3 py-2.5 text-[13px] leading-relaxed text-ink placeholder:text-ink-3"
              />
              <div className="mt-3 flex flex-wrap items-center gap-3">
                <Button
                  kind="primary"
                  data-testid="personality-save"
                  disabled={!authed}
                  onClick={savePersonality}
                  className="text-[12px]"
                >
                  Save personality
                </Button>
                <span
                  data-testid="personality-status"
                  className={`text-[12px] ${personalityStatus.bad ? "text-danger" : "text-ink-3"}`}
                >
                  {personalityStatus.text}
                </span>
              </div>
            </div>
          )}
        </SettingsSection>
      );
    }

    if (section.key === "access") {
      return (
        <SettingsSection
          key="access"
          id="access"
          title="Access"
          note="The token this browser sends with every request. It is stored only in local storage on this device."
        >
          <SettingsRow
            label="API token"
            hint="Required to reach the chat, approvals, activity, audit and policy endpoints."
            hideHint={!showHints}
          >
            <div className="flex flex-wrap items-center justify-end gap-2">
              <QuietInput
                type="password"
                placeholder="API token"
                value={tokenValue}
                onChange={(e) => setTokenValue(e.target.value)}
                className="quiet-field-edge w-56"
              />
              <Button kind="primary" onClick={saveToken} className="text-[12px]">
                Save
              </Button>
              <Button onClick={clearToken} className="text-[12px]">
                Clear
              </Button>
            </div>
          </SettingsRow>
        </SettingsSection>
      );
    }

    const view = feedViews[section.key];
    if (!view) return null;
    const surface = section.key === "messaging" || section.key === "mcp_servers";
    const emptySurface = surface && view.fields.length === 0;
    const onCount = view.fields.filter((field) => Boolean(field.value)).length;
    return (
      <SettingsSection
        key={section.key}
        id={section.key}
        title={section.title}
        note={view.group.note}
        badge={surface && !emptySurface ? `${onCount} on` : null}
      >
        {emptySurface ? (
          <div className="settings-row py-4 text-[12.5px] text-ink-3">
            {section.key === "messaging"
              ? "No chat surfaces are connected yet."
              : "No app servers are set up yet."}
          </div>
        ) : surface ? (
          surfaceTable(view, section.key === "messaging" ? "Chat" : "App server")
        ) : (
          view.fields.map(fieldRow)
        )}
      </SettingsSection>
    );
  };

  const mobileNav = (
    <div className="flex flex-col gap-2">
      <SettingsSearch query={query} onQuery={setQuery} testId="settings-search-mobile" />
      <label className="flex items-center gap-2">
        <span className="sr-only">Settings section</span>
        <select
          value={active}
          onChange={(e) => goSection(e.target.value)}
          aria-label="Settings section"
          className="aether-field aether-select w-full px-3 py-1.5 text-sm text-ink"
        >
          {availableSections.map((section) => (
            <option key={section.key} value={section.key}>
              {section.title}
            </option>
          ))}
        </select>
      </label>
    </div>
  );

  return (
    <div className="settings-stage relative -mx-4 -my-6 min-h-[calc(100dvh-52px)] px-4 min-[901px]:-mx-6 min-[901px]:px-6">
      <div className="mx-auto w-full max-w-[1680px]">
        <SettingsWorkspace
          mobileNav={mobileNav}
          rail={
            <SettingsRail
              groups={railGroups}
              active={active}
              query={query}
              onQuery={setQuery}
              onSelect={goSection}
            />
          }
        >
          <SettingsContent>
            {!q && authed && feedError ? (
              <div className="pt-4">
                <SettingsNotice title="Configuration isn't available.">{feedError}</SettingsNotice>
              </div>
            ) : null}
            {!q && authed && !feed && !feedError ? (
              <div className="pt-4">
                <SettingsNotice title="Reading configuration">
                  The editable surface arrives from the backend in a moment.
                </SettingsNotice>
              </div>
            ) : null}
            {visibleSections.length > 0 ? (
              visibleSections.map(renderSection)
            ) : (
              <div className="pt-4">
                <SettingsNotice title="Nothing matches that search">
                  {authed
                    ? "Try a plain word like theme, model, token, or quiet hours."
                    : "General and Access are open until the API token is set."}
                </SettingsNotice>
              </div>
            )}
          </SettingsContent>
        </SettingsWorkspace>
      </div>
    </div>
  );
}
