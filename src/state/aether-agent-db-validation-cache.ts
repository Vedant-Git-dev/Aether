import path from "node:path";
import { isPathInside } from "../infra/path-guards.js";
import { resolveGlobalSingleton } from "../shared/global-singleton.js";

// Physical close retains validation for cheap reopens. Registry lifecycle changes
// revoke it across native and transformed module graphs before a replacement opens.
const validatedPaths = resolveGlobalSingleton<Map<string, string>>(
  Symbol.for("aether.agentDatabaseValidatedPaths"),
  () => new Map(),
);

export function getValidatedAetherAgentDatabaseOwner(pathname: string): string | undefined {
  return validatedPaths.get(path.resolve(pathname));
}

export function setValidatedAetherAgentDatabaseOwner(pathname: string, agentId: string): void {
  validatedPaths.set(path.resolve(pathname), agentId);
}

export function invalidateAetherAgentDatabaseValidation(pathname: string): void {
  validatedPaths.delete(path.resolve(pathname));
}

export function invalidateAetherAgentDatabaseValidationsForAgent(agentId: string): void {
  for (const [pathname, validatedAgentId] of validatedPaths) {
    if (validatedAgentId === agentId) {
      validatedPaths.delete(pathname);
    }
  }
}

export function clearAetherAgentDatabaseValidationCache(rootPath?: string): void {
  for (const pathname of validatedPaths.keys()) {
    if (rootPath === undefined || isPathInside(rootPath, pathname)) {
      validatedPaths.delete(pathname);
    }
  }
}
