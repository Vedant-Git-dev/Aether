// Summarizes heartbeat config for CLI and UI display.
import type { AetherConfig } from "../config/types.aether.js";
import { resolveHeartbeatIntervalMs } from "./heartbeat-config.js";
import {
  buildHeartbeatSummary,
  enrolledHeartbeatAgentIds,
  isEnrolledHeartbeatAgent,
  type HeartbeatSummary,
} from "./heartbeat-summary-projection.js";

export { resolveHeartbeatIntervalMs };
export type { HeartbeatSummary };

/** Return whether heartbeat scheduling applies to an agent. */
export function isHeartbeatEnabledForAgent(cfg: AetherConfig, agentId?: string): boolean {
  return isEnrolledHeartbeatAgent(cfg, agentId, enrolledHeartbeatAgentIds(cfg));
}

/** Resolve display-ready heartbeat settings for an agent. */
export function resolveHeartbeatSummaryForAgent(
  cfg: AetherConfig,
  agentId?: string,
): HeartbeatSummary {
  return buildHeartbeatSummary(cfg, agentId, enrolledHeartbeatAgentIds(cfg));
}
