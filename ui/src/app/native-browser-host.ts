export function hasNativeBrowserBridge(): boolean {
  const host:
    | (Window & {
        webkit?: { messageHandlers?: { aetherBrowser?: { postMessage?: unknown } } };
      })
    | undefined = typeof window === "undefined" ? undefined : window;
  return typeof host?.webkit?.messageHandlers?.aetherBrowser?.postMessage === "function";
}
