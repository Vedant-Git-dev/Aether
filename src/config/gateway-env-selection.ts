import { collectConfigRuntimeEnvVars } from "./env-vars.js";
import type { AetherConfig } from "./types.js";

export const GATEWAY_CONFIG_SELECTION_ENV_KEYS: ReadonlySet<string> = new Set([
  "HOME",
  "HOMEDRIVE",
  "HOMEPATH",
  "AETHER_AGENT_DIR",
  "AETHER_CONFIG_PATH",
  "AETHER_HOME",
  "AETHER_INCLUDE_ROOTS",
  "AETHER_CONFIG_READONLY",
  "AETHER_NIX_MODE",
  "AETHER_OAUTH_DIR",
  "AETHER_PACKAGE_DIR",
  "AETHER_PROFILE",
  "AETHER_STATE_DIR",
  "AETHER_WORKSPACE_DIR",
  "PI_CODING_AGENT_DIR",
  "PREFIX",
  "USERPROFILE",
]);

/** Rejects config.env changes that would retarget a running Gateway process. */
export function assertGatewayConfigEnvSelectionUnchanged(
  previousConfig: AetherConfig,
  nextConfig: AetherConfig,
): void {
  const normalize = (config: AetherConfig) =>
    new Map(
      Object.entries(collectConfigRuntimeEnvVars(config)).map(([key, value]) => [
        key.toUpperCase(),
        value,
      ]),
    );
  const previous = normalize(previousConfig);
  const next = normalize(nextConfig);
  for (const key of GATEWAY_CONFIG_SELECTION_ENV_KEYS) {
    if (previous.get(key) !== next.get(key)) {
      throw new Error(
        `Config env cannot change process-stable Gateway selector ${key} during reload. Restart with the target environment instead.`,
      );
    }
  }
}
