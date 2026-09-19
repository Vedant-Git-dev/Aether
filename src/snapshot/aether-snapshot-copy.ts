import { containsAsciiControlCharacter } from "@aether/normalization-core/string-normalization";
import { resolveStateDir } from "../config/paths.js";
import {
  createVerifiedSqliteSnapshot,
  type SqliteSnapshotValidator,
} from "../infra/sqlite-snapshot.js";
import { assertNotUpdateCapturePath } from "../infra/update-capture-paths.js";
import { isValidAgentId, normalizeAgentId } from "../routing/session-key.js";
import { assertAetherAgentDatabaseForMaintenance } from "../state/aether-agent-db.js";
import { assertAetherStateDatabaseForMaintenance } from "../state/aether-state-db.js";
import {
  sanitizeAetherGlobalStateSnapshot,
  sanitizeAetherStateLeaseRows,
} from "../state/aether-state-snapshot-sanitizer.js";
import type { SnapshotDatabaseIdentity, SnapshotDatabaseRef } from "./snapshot-provider.js";

export function normalizeSnapshotIdentity(
  identity: SnapshotDatabaseIdentity,
): SnapshotDatabaseIdentity {
  if (identity.role === "global") {
    return identity;
  }
  if (identity.role === "agent") {
    const agentId = normalizeAgentId(identity.agentId);
    if (!isValidAgentId(identity.agentId) || agentId !== identity.agentId) {
      throw new Error(`SQLite snapshot agent id must be canonical: ${identity.agentId}`);
    }
    return { role: "agent", agentId };
  }
  const id = identity.id.trim();
  if (!id || id !== identity.id || id.length > 256 || containsAsciiControlCharacter(id)) {
    throw new Error("SQLite snapshot generic database id is invalid.");
  }
  return { role: "generic", id };
}

export function buildSnapshotValidator(
  identity: SnapshotDatabaseIdentity,
): SqliteSnapshotValidator {
  if (identity.role === "global") {
    return (database, pathname) =>
      assertAetherStateDatabaseForMaintenance(database, { pathname });
  }
  if (identity.role === "agent") {
    return (database, pathname) =>
      assertAetherAgentDatabaseForMaintenance(database, {
        agentId: identity.agentId,
        pathname,
      });
  }
  return () => undefined;
}

/** Produce the canonical sanitized, compact, verified copy used by every snapshot provider. */
export async function createAetherSnapshotCopy(params: {
  database: SnapshotDatabaseRef;
  targetPath: string;
}): Promise<{ identity: SnapshotDatabaseIdentity; path: string; userVersion: number }> {
  assertNotUpdateCapturePath(params.database.path, resolveStateDir());
  const identity = normalizeSnapshotIdentity(params.database.identity);
  const result = await createVerifiedSqliteSnapshot({
    sourcePath: params.database.path,
    targetPath: params.targetPath,
    requireNonEmptySource: identity.role !== "generic",
    transform:
      identity.role === "global"
        ? sanitizeAetherGlobalStateSnapshot
        : identity.role === "agent"
          ? sanitizeAetherStateLeaseRows
          : undefined,
    validate: buildSnapshotValidator(identity),
  });
  return { identity, ...result };
}
