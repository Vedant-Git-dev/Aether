import { useCallback, useEffect, useState } from "react";

const THEME_KEY = "aether_theme";

export function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function applyTheme(t, persist) {
  document.documentElement.dataset.theme = t;
  if (persist) {
    try { localStorage.setItem(THEME_KEY, t); } catch { /* private mode */ }
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = t === "dark" ? "#17181a" : "#f7f7f8";
}

// The pre-paint script in index.html sets data-theme before first paint; this
// hook keeps the toggle button, the meta theme-color and (optionally) the
// saved preference in sync with it. Follows the system theme only until the
// user picks one explicitly — same contract as the old panel.
export function useTheme() {
  const [theme, setTheme] = useState(currentTheme);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (e) => {
      let saved = null;
      try { saved = localStorage.getItem(THEME_KEY); } catch { /* private mode */ }
      if (saved !== "light" && saved !== "dark") {
        applyTheme(e.matches ? "dark" : "light", false);
        setTheme(e.matches ? "dark" : "light");
      }
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const toggle = useCallback(() => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next, true);
    setTheme(next);
  }, []);

  return { theme, toggle };
}
