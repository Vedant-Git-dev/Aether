import { expectDefined } from "@aether/normalization-core";
// On roughly one day in sixteen the interactive banner gains a tiny ASCII
// rendering of the Aether orb. The day comes from the shared lobster-day hash
// so every surface agrees on the calendar and tests can pin dates.
import { isLobsterDay, lobsterDayHash } from "../shared/lobster-day.js";

const ORB_ARTS: readonly string[] = [
  // The faceted orb, full view.
  [
    "      _.---._",
    "    .' /\\ /\\ '.",
    "   | /  \\/  \\  |",
    "   | \\  /\\  /  |",
    "    '. \\/\\/ .'",
    "      '-----'",
  ].join("\n"),
  // A smaller, brighter glint of the orb.
  ["    .-===-.", "   / /\\/\\ \\", "   \\ \\/\\/ /", "    '-===-'"].join("\n"),
] as const;

/**
 * Return the ASCII orb for `now`'s calendar day, or null on non-orb
 * days and in CI/test environments (banner tests assert exact bytes).
 */
export function pickCliLobsterArt(now: Date, env: NodeJS.ProcessEnv = process.env): string | null {
  if (env.CI || env.VITEST) {
    return null;
  }
  if (!isLobsterDay(now)) {
    return null;
  }
  return expectDefined(
    ORB_ARTS[(lobsterDayHash(now) >>> 8) % ORB_ARTS.length],
    "orb arts entry at (lobster day hash(now) >>> 8) % orb arts.length",
  );
}
