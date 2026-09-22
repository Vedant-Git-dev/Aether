const PLATFORM_DISPLAY_NAMES: Record<string, string> = {
  macos: "macOS",
  darwin: "macOS",
  win32: "Windows",
  windows: "Windows",
  linux: "Linux",
  web: "Web",
};

export function prettifyPlatform(platform: string, deviceFamily?: string | null): string {
  const [name = "", ...rest] = platform.trim().split(/\s+/u);
  // MacIntel alone also identifies legacy unclassified records.
  if (name.toLowerCase() === "macintel" && deviceFamily === "Mac") {
    return ["macOS", ...rest].join(" ");
  }
  // Mixed-case names ("macOS") are already branded; only capitalize all-lowercase input.
  const fallback =
    name === name.toLowerCase() ? `${name.charAt(0).toUpperCase()}${name.slice(1)}` : name;
  const displayName = PLATFORM_DISPLAY_NAMES[name.toLowerCase()] ?? fallback;
  return [displayName, ...rest].join(" ");
}
