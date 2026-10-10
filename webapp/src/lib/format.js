// Time and hash formatting — ported unchanged from the old panel.
export function fmtTime(iso) {
  if (!iso) return "—";
  // the scheduler records run_at as "YYYY-MM-DD HH:MM:SS+ZZZZ"; normalize to ISO
  const text = String(iso).replace(
    /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})([+-]\d{2})(\d{2})$/,
    "$1T$2$3:$4",
  );
  const d = new Date(text);
  if (Number.isNaN(d.getTime())) return text;
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function fmtDuration(fromIso, toIso) {
  const from = new Date(fromIso).getTime();
  const to = toIso ? new Date(toIso).getTime() : Date.now();
  if (Number.isNaN(from)) return "—";
  const s = Math.max(0, Math.round((to - from) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}

export function shortHash(hash) {
  if (!hash) return "—";
  return hash.length > 14 ? `${hash.slice(0, 6)}…${hash.slice(-6)}` : hash;
}
