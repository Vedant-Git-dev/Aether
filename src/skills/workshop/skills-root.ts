import path from "node:path";
import { resolveAgentDir } from "../../agents/agent-scope-config.js";
import type { AetherConfig } from "../../config/types.aether.js";

export function resolveWorkshopSkillsDir(
  config: AetherConfig,
  agentId: string,
  env: NodeJS.ProcessEnv = process.env,
): string {
  return path.join(resolveAgentDir(config, agentId, env), "workshop-skills");
}

export function resolveWorkshopWatchRoots(config?: AetherConfig, agentId?: string) {
  return config && agentId
    ? [{ path: resolveWorkshopSkillsDir(config, agentId), source: "aether-workshop" }]
    : [];
}
