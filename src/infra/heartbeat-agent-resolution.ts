import { tryResolveAmbientOwnerAgentId } from "../agents/agent-scope-config.js";
import type { AetherConfig } from "../config/types.aether.js";

export function tryResolveAmbientHeartbeatAgentId(cfg: AetherConfig): string | undefined {
  return tryResolveAmbientOwnerAgentId(cfg, cfg.agents?.defaults?.heartbeat?.agentId);
}
