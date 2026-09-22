import path from "node:path";
import { resolveConfigPath } from "../config/paths.js";
import { resolveAetherStateSqlitePath } from "../state/aether-state-db.paths.js";
import type { ClawMonitorCleanupBinding } from "./monitor-cleanup-contract.js";

/** Claw files remain local; the serving monitor owner must use that same state and config. */
export function resolveClawMonitorCleanupBinding(cronStorePath: string): ClawMonitorCleanupBinding {
  return {
    configPath: path.resolve(resolveConfigPath()),
    statePath: path.resolve(resolveAetherStateSqlitePath()),
    cronStorePath: path.resolve(cronStorePath),
  };
}
