// Msteams plugin module implements user agent behavior.
import { createRequire } from "node:module";
import { getMSTeamsRuntime } from "./runtime.js";

let cachedUserAgent: string | undefined;

function resolveTeamsSdkVersion(): string {
  try {
    const require = createRequire(import.meta.url);
    const pkg = require("@microsoft/teams.apps/package.json") as { version?: string };
    return pkg.version ?? "unknown";
  } catch {
    return "unknown";
  }
}

function resolveAetherVersion(): string {
  try {
    return getMSTeamsRuntime().version;
  } catch {
    return "unknown";
  }
}

/**
 * Build a combined User-Agent string that preserves the Teams SDK identity
 * and appends the Aether version.
 *
 * Format: "teams.ts[apps]/<sdk-version> Aether/<aether-version>"
 * Example: "teams.ts[apps]/2.0.5 Aether/2026.3.22"
 *
 * This lets the Teams backend track SDK usage while also identifying the
 * host application.
 */
export function buildUserAgent(): string {
  if (cachedUserAgent) {
    return cachedUserAgent;
  }
  cachedUserAgent = `teams.ts[apps]/${resolveTeamsSdkVersion()} Aether/${resolveAetherVersion()}`;
  return cachedUserAgent;
}

/**
 * User-Agent fragment for the Teams SDK App's client. The SDK's Client.clone
 * merges this with its own `teams.ts[apps]/<sdk-version>` identifier, so we
 * only contribute the Aether piece — passing the full `buildUserAgent()`
 * would double-print the SDK token.
 *
 * Format: "Aether/<aether-version>"
 */
export function buildAetherUserAgentFragment(): string {
  return `Aether/${resolveAetherVersion()}`;
}

export function ensureUserAgentHeader(headers?: HeadersInit): Headers {
  const nextHeaders = new Headers(headers);
  if (!nextHeaders.has("User-Agent")) {
    nextHeaders.set("User-Agent", buildUserAgent());
  }
  return nextHeaders;
}
