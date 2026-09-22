import { asOptionalRecord } from "@aether/normalization-core/record-coerce";
import { getRuntimeConfigSnapshot, getRuntimeConfigSourceSnapshot } from "./runtime-snapshot.js";
import { projectRuntimeChangesOntoSource } from "./source-value-projection.js";
import type { AetherConfig } from "./types.js";

/** Projects a runtime-derived config back onto the active authored source snapshot. */
export function projectConfigOntoRuntimeSourceSnapshot(config: AetherConfig): AetherConfig {
  const runtimeConfigSnapshot = getRuntimeConfigSnapshot();
  const runtimeConfigSourceSnapshot = getRuntimeConfigSourceSnapshot();
  if (!runtimeConfigSnapshot || !runtimeConfigSourceSnapshot) {
    return config;
  }
  if (config === runtimeConfigSnapshot) {
    return runtimeConfigSourceSnapshot;
  }
  const runtime = runtimeConfigSnapshot as Record<string, unknown>;
  const candidate = config as Record<string, unknown>;
  for (const key of Object.keys(runtime)) {
    if (!Object.hasOwn(candidate, key)) {
      return config;
    }
    const runtimeValue = runtime[key];
    const candidateValue = candidate[key];
    if (
      Array.isArray(runtimeValue) !== Array.isArray(candidateValue) ||
      (runtimeValue === null) !== (candidateValue === null) ||
      typeof runtimeValue !== typeof candidateValue
    ) {
      return config;
    }
  }
  return projectRuntimeChangesOntoSource(
    runtimeConfigSourceSnapshot,
    runtimeConfigSnapshot,
    config,
  ) as AetherConfig;
}

/** Projects partial legacy inputs without persisting deleted runtime-only parents. */
export function projectLegacyRuntimeConfigWrite(
  config: AetherConfig,
  runtimeSnapshot = getRuntimeConfigSnapshot(),
  sourceSnapshot = getRuntimeConfigSourceSnapshot(),
): AetherConfig {
  if (!runtimeSnapshot || !sourceSnapshot) {
    return config;
  }
  // Legacy partial writes alone omit parents created only by removing runtime defaults.
  return (asOptionalRecord(
    projectRuntimeChangesOntoSource(sourceSnapshot, runtimeSnapshot, config, {
      pruneUnauthoredDeletions: true,
    }),
  ) ?? {}) as AetherConfig;
}
