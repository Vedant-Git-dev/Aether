import { useState } from "react";

// One id's action in flight at a time, cleared whatever happens — the
// shared shape behind every "tap a button, call the API, clear the busy
// flag" row in the panel (a task verdict, a profile confirm, ...).
export function useBusyAction() {
  const [busy, setBusy] = useState({});

  const run = async (id, label, fn) => {
    setBusy((b) => ({ ...b, [id]: label }));
    try {
      return await fn();
    } finally {
      setBusy((b) => ({ ...b, [id]: false }));
    }
  };

  return [busy, run];
}
