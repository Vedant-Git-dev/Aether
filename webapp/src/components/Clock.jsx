// The only per-second state in the app, isolated here so nothing else
// re-renders on the tick.
import { useEffect, useState } from "react";
import Icon from "./Icon.jsx";

export default function Clock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const day = now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  const pad = (n) => String(n).padStart(2, "0");
  const ampm = now.getHours() >= 12 ? "pm" : "am";
  const h12 = now.getHours() % 12 || 12;

  return (
    <div className="hidden items-center gap-1.5 text-xs text-ink-3 min-[701px]:flex">
      <Icon name="clock" className="h-3.5 w-3.5" />
      <span>{day} · {h12}:{pad(now.getMinutes())}:{pad(now.getSeconds())} {ampm}</span>
    </div>
  );
}
