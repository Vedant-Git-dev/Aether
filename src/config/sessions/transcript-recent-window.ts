import { asFiniteNumber } from "@aether/normalization-core/number-coercion";

export const normalizeTranscriptTimestamp = asFiniteNumber;

export function isWithinTranscriptWindow(
  timestamp: number | undefined,
  options: { beforeTimestampMs?: number; minTimestampMs?: number },
): boolean {
  return (
    (options.beforeTimestampMs === undefined ||
      timestamp === undefined ||
      timestamp < options.beforeTimestampMs) &&
    (options.minTimestampMs === undefined ||
      timestamp === undefined ||
      timestamp >= options.minTimestampMs)
  );
}

export function normalizeRecentTranscriptLimit(limit: number | undefined): number {
  return Math.max(1, Math.floor(limit ?? 10));
}

export function readPreferredUpstreamUserText(message: {
  __aether?: unknown;
}): string | null | undefined {
  const meta =
    message["__aether"] && typeof message["__aether"] === "object"
      ? (message["__aether"] as Record<string, unknown>)
      : undefined;
  if (typeof meta?.upstreamUserText === "string") {
    return meta.upstreamUserText.trim();
  }
  return meta?.mirrorOrigin ? null : undefined;
}
