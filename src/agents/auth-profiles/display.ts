/**
 * Auth profile display labels.
 * Combines profile ids with configured human metadata for CLI/status output.
 */
import type { AetherConfig } from "../../config/types.aether.js";
import { resolveAuthProfileMetadata } from "./identity.js";
import type { AuthProfileStore } from "./types.js";

/** Builds the human-readable profile label used in status and auth listings. */
export function resolveAuthProfileDisplayLabel(params: {
  cfg?: AetherConfig;
  store: AuthProfileStore;
  profileId: string;
}): string {
  const { displayName, email } = resolveAuthProfileMetadata(params);
  if (displayName) {
    return `${params.profileId} (${displayName})`;
  }
  if (email) {
    return `${params.profileId} (${email})`;
  }
  return params.profileId;
}
