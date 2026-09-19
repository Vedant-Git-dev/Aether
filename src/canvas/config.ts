/** Core Canvas host enablement from the shipped Canvas plugin configuration surface. */
import { isRecord } from "@aether/normalization-core/record-coerce";
import type { AetherConfig } from "../config/types.aether.js";
import { isTruthyEnvValue } from "../infra/env.js";

/** Returns whether core-owned widget hosting and tools should be active. */
export function isCoreCanvasHostEnabled(
  config?: AetherConfig,
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  // Canvas owned these shipped operator switches before hosting moved into core.
  // Core keeps reading them so existing disablement still covers the whole Canvas family.
  if (isTruthyEnvValue(env.AETHER_SKIP_CANVAS_HOST)) {
    return false;
  }
  const host = config?.plugins?.entries?.canvas?.config?.host;
  return !isRecord(host) || host.enabled !== false;
}
