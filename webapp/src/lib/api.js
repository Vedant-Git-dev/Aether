// The one fetch helper: token rides as a query param on every call (same
// contract as the old panel), 204 maps to null, and FastAPI's `detail`
// field is extracted for error text.
const TOKEN_KEY = "aether_token";

export const token = () => localStorage.getItem(TOKEN_KEY) || "";
export const hasToken = () => Boolean(token());

export async function api(path, opts) {
  const sep = path.includes("?") ? "&" : "?";
  const res = await fetch(`${path}${sep}token=${encodeURIComponent(token())}`, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail || `request failed (${res.status})`);
  }
  if (res.status === 204) return null;
  return res.json();
}
