import type { AetherConfig } from "aether/plugin-sdk/config-contracts";

export function resolveZalouserDmSessionScope(config: AetherConfig) {
  const configured = config.session?.dmScope;
  return configured === "main" || !configured ? "per-channel-peer" : configured;
}
