// Normalizes config version metadata and compatibility comparisons.
import { parse as parseSemver, type SemVer } from "semver";
import {
  compareAetherSemver,
  isAetherCorrectionSemver,
  normalizeLegacyDotBetaVersion,
} from "../infra/semver.js";

/** Parses stable, prerelease, and legacy dot-beta Aether versions. */
function parseAetherVersion(raw: string | null | undefined): SemVer | null {
  if (!raw) {
    return null;
  }
  const normalized = normalizeLegacyDotBetaVersion(raw.trim());
  return parseSemver(normalized);
}

export function normalizeAetherVersionBase(raw: string | null | undefined): string | null {
  const parsed = parseAetherVersion(raw);
  if (!parsed) {
    return null;
  }
  return `${parsed.major}.${parsed.minor}.${parsed.patch}`;
}

export function compareAetherVersions(
  a: string | null | undefined,
  b: string | null | undefined,
): number | null {
  const parsedA = parseAetherVersion(a);
  const parsedB = parseAetherVersion(b);
  if (!parsedA || !parsedB) {
    return null;
  }
  return compareAetherSemver(parsedA, parsedB);
}

export function shouldWarnOnTouchedVersion(
  current: string | null | undefined,
  touched: string | null | undefined,
): boolean {
  const parsedCurrent = parseAetherVersion(current);
  const parsedTouched = parseAetherVersion(touched);
  if (parsedCurrent && parsedTouched && parsedCurrent.compareMain(parsedTouched) === 0) {
    if (parsedTouched.prerelease.length === 0 || isAetherCorrectionSemver(parsedTouched)) {
      return false;
    }
  }
  return parsedCurrent !== null && parsedTouched !== null
    ? compareAetherSemver(parsedCurrent, parsedTouched) < 0
    : false;
}
