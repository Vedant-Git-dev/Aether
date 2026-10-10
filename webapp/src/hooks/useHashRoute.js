import { useEffect, useState } from "react";

// The whole router: #/<name> -> page name, "activity" when absent or unknown.
// react-router would weigh more than these lines.
export function useHashRoute(validPages) {
  const parse = () => {
    const m = /^#\/(\w+)/.exec(location.hash);
    const p = m && validPages.includes(m[1]) ? m[1] : "activity";
    return p;
  };
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const onChange = () => setRoute(parse());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return route;
}
