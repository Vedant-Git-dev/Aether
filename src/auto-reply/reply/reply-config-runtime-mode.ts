import type { AetherConfig } from "../../config/types.aether.js";

// Reply completeness is process-local metadata. Keep it off config objects so
// frozen runtime snapshots and identity-keyed caches remain valid.
const replyConfigRuntimeModes = new WeakMap<AetherConfig, "fast" | "full">();

export function markReplyConfigRuntimeMode<T extends AetherConfig>(
  config: T,
  runtimeMode: "fast" | "full",
): T {
  replyConfigRuntimeModes.set(config, runtimeMode);
  return config;
}

export function isCompleteReplyConfig(config: unknown): config is AetherConfig {
  return Boolean(
    config && typeof config === "object" && replyConfigRuntimeModes.has(config as AetherConfig),
  );
}

export function usesFullReplyRuntime(config: unknown): boolean {
  if (!config || typeof config !== "object") {
    return false;
  }
  const mode = replyConfigRuntimeModes.get(config as AetherConfig);
  return mode === "full";
}
