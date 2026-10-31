import { useCallback, useEffect, useState } from "react";

const THEME_KEY = "aether_theme";
const THEME_EVENT = "aether:theme";

export function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

// "system" is a real preference, not an absence: choosing it keeps following
// the OS until the user picks a side explicitly.
export function currentPreference() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === "light" || saved === "dark" || saved === "system") return saved;
  } catch {
    /* private mode */
  }
  return "system";
}

function systemTheme() {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

function paint(theme) {
  document.documentElement.dataset.theme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = theme === "dark" ? "#111318" : "#f4f5f8";
}

// Applies a preference, stores it, and tells every other useTheme instance
// (the topbar and the settings page both own one) before returning the
// resolved light/dark value.
function applyPreference(preference, persist) {
  const theme = preference === "system" ? systemTheme() : preference;
  paint(theme);
  if (persist) {
    try {
      localStorage.setItem(THEME_KEY, preference);
    } catch {
      /* private mode */
    }
  }
  window.dispatchEvent(
    new CustomEvent(THEME_EVENT, { detail: { preference, theme } })
  );
  return theme;
}

export function useTheme() {
  const [preference, setPreferenceState] = useState(currentPreference);
  const [theme, setTheme] = useState(currentTheme);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onSystemChange = () => {
      if (currentPreference() !== "system") return;
      const next = systemTheme();
      paint(next);
      setTheme(next);
    };
    const onTheme = (event) => {
      setPreferenceState(event.detail.preference);
      setTheme(event.detail.theme);
    };
    mq.addEventListener("change", onSystemChange);
    window.addEventListener(THEME_EVENT, onTheme);
    return () => {
      mq.removeEventListener("change", onSystemChange);
      window.removeEventListener(THEME_EVENT, onTheme);
    };
  }, []);

  const setPreference = useCallback((next) => {
    const resolved = applyPreference(next, true);
    setPreferenceState(next);
    setTheme(resolved);
  }, []);

  const toggle = useCallback(() => {
    setPreference(currentTheme() === "dark" ? "light" : "dark");
  }, [setPreference]);

  return { theme, preference, setPreference, toggle };
}
