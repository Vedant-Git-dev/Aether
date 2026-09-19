import path from "node:path";
import { resolveAgentDir } from "../../agents/agent-scope-config.js";
import type { AetherConfig } from "../../config/types.aether.js";

const BACKUP_REL_DIR = path.join("skill-workshop", "collection-backups");

export function resolveSkillCollectionBackupRoot(
  config: AetherConfig,
  agentId: string,
  env?: NodeJS.ProcessEnv,
): string {
  return path.join(resolveAgentDir(config, agentId, env), BACKUP_REL_DIR);
}
