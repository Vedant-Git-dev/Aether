import { lstatSync, statSync } from "node:fs";
import path from "node:path";
import { executeSqliteQuerySync, getNodeSqliteKysely } from "../infra/kysely-sync.js";
import { resolveSqliteDatabaseFilePaths } from "../infra/sqlite-files.js";
import { normalizeAgentId } from "../routing/session-key.js";
import { resolveGlobalSingleton } from "../shared/global-singleton.js";
import {
  AETHER_AGENT_SCHEMA_VERSION,
  type AetherRegisteredAgentDatabase,
} from "./aether-agent-db-contract.js";
import {
  withExistingAetherStateDatabaseArtifactPreservingReadOnly,
  withExistingAetherStateDatabaseReadOnly,
} from "./aether-state-db-readonly.js";
import { detectAetherStateDatabaseSchemaMigrationsFromDatabase } from "./aether-state-db-schema-repair.js";
import type { DB as AetherStateKyselyDatabase } from "./aether-state-db.generated.js";
import type { AetherStateDatabaseOptions } from "./aether-state-db.js";
import {
  resolveAetherRegisteredAgentDatabasePath,
  resolveAetherStateSqlitePath,
} from "./aether-state-db.paths.js";

type AetherAgentRegistryDatabase = Pick<AetherStateKyselyDatabase, "agent_databases">;

// Registry metadata is process-stable: registry writes invalidate after each commit;
// other-process changes take effect on restart. Polling here puts schema probes back on hot reads.
type AgentDatabaseRegistryMemo = {
  pathname: string;
  token: symbol;
  entries?: readonly AetherRegisteredAgentDatabase[];
};
// A plugin may first open a hot-created agent; its registration must invalidate
// native discovery even when subsequent callers reuse the shared connection.
const registry = resolveGlobalSingleton<{ memo?: AgentDatabaseRegistryMemo }>(
  Symbol.for("aether.agentDatabaseRegistryMemo"),
  () => ({}),
);

function resolveAgentDatabaseRegistryPath(options: AetherStateDatabaseOptions): string {
  return path.resolve(options.path ?? resolveAetherStateSqlitePath(options.env ?? process.env));
}

function activateRegisteredAgentDatabasesMemo(
  options: AetherStateDatabaseOptions,
): AgentDatabaseRegistryMemo {
  const pathname = resolveAgentDatabaseRegistryPath(options);
  if (registry.memo?.pathname !== pathname) {
    // One active pathname keeps registry metadata process-stable without retaining
    // an unbounded generation map. Switching back creates a fresh generation.
    registry.memo = { pathname, token: Symbol(pathname) };
  }
  return registry.memo;
}

/** Return the process-stable generation for the active agent database registry. */
export function readAetherAgentDatabaseRegistryToken(
  options: AetherStateDatabaseOptions = {},
): symbol {
  return activateRegisteredAgentDatabasesMemo(options).token;
}

export function invalidateRegisteredAgentDatabasesMemo(
  options: AetherStateDatabaseOptions,
): void {
  const pathname = resolveAgentDatabaseRegistryPath(options);
  if (registry.memo?.pathname === pathname) {
    registry.memo = { pathname, token: Symbol(pathname) };
  }
}

function cloneRegisteredAgentDatabases(
  entries: readonly AetherRegisteredAgentDatabase[],
): AetherRegisteredAgentDatabase[] {
  return entries.map((entry) => ({ ...entry }));
}

function hasUnavailableMissingSqlitePath(pathname: string): boolean {
  for (const candidate of resolveSqliteDatabaseFilePaths(pathname)) {
    try {
      lstatSync(candidate);
      return true;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
        return true;
      }
    }
  }

  let ancestor = path.dirname(pathname);
  while (true) {
    try {
      const stat = lstatSync(ancestor);
      if (!stat.isSymbolicLink()) {
        return !stat.isDirectory();
      }
      try {
        return !statSync(ancestor).isDirectory();
      } catch {
        return true;
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
        return true;
      }
    }
    const parent = path.dirname(ancestor);
    if (parent === ancestor) {
      return false;
    }
    ancestor = parent;
  }
}

type AgentDatabaseRegistryListOptions = AetherStateDatabaseOptions & {
  includeIncompatibleSchemaVersions?: boolean;
};

function readRegisteredAgentDatabases(
  options: AgentDatabaseRegistryListOptions,
  artifactPreserving: boolean,
): AetherRegisteredAgentDatabase[] {
  const pathname = resolveAgentDatabaseRegistryPath(options);
  const read = ({ db: database }: { db: import("node:sqlite").DatabaseSync }) => {
    const schemaMigrations = detectAetherStateDatabaseSchemaMigrationsFromDatabase(
      database,
      pathname,
    );
    if (!artifactPreserving && schemaMigrations.length > 0) {
      throw new Error(
        `Aether state database ${pathname} has a legacy agent database registry schema; run aether vitals --fix to migrate it.`,
      );
    }
    const registryTable = database
      .prepare("SELECT type FROM sqlite_master WHERE name = 'agent_databases'")
      .get() as { type?: unknown } | undefined;
    if (!registryTable) {
      return [];
    }
    if (registryTable.type !== "table") {
      throw new Error(`Aether state database ${pathname} has an invalid agent registry.`);
    }
    const db = getNodeSqliteKysely<AetherAgentRegistryDatabase>(database);
    return executeSqliteQuerySync(
      database,
      db
        .selectFrom("agent_databases")
        .selectAll()
        .orderBy("agent_id", "asc")
        .orderBy("path", "asc"),
    ).rows.map((row) => ({
      agentId: normalizeAgentId(row.agent_id),
      path: resolveAetherRegisteredAgentDatabasePath(pathname, row.path),
      schemaVersion: row.schema_version,
      lastSeenAt: row.last_seen_at,
      sizeBytes: row.size_bytes,
    }));
  };
  const entries = artifactPreserving
    ? withExistingAetherStateDatabaseArtifactPreservingReadOnly(read, options)
    : withExistingAetherStateDatabaseReadOnly(read, options);
  if (entries === undefined) {
    if (hasUnavailableMissingSqlitePath(pathname)) {
      throw new Error(`Aether state database ${pathname} is unavailable.`);
    }
    return [];
  }
  return options.includeIncompatibleSchemaVersions
    ? entries
    : entries.filter((entry) => entry.schemaVersion === AETHER_AGENT_SCHEMA_VERSION);
}

/** Inspect a copied registry without creating SQLite artifacts or runtime memo state. */
export function inspectAetherRegisteredAgentDatabases(
  options: AgentDatabaseRegistryListOptions = {},
): AetherRegisteredAgentDatabase[] {
  return readRegisteredAgentDatabases(options, true);
}

/** List agent databases recorded in the shared Aether state registry. */
export function listAetherRegisteredAgentDatabases(
  options: AgentDatabaseRegistryListOptions = {},
): AetherRegisteredAgentDatabase[] {
  const memo = activateRegisteredAgentDatabasesMemo(options);
  if (memo.entries) {
    const entries = cloneRegisteredAgentDatabases(memo.entries);
    return options.includeIncompatibleSchemaVersions
      ? entries
      : entries.filter((entry) => entry.schemaVersion === AETHER_AGENT_SCHEMA_VERSION);
  }
  // Discovery runs per row in list hot paths, so the legacy-schema gate and the
  // query share one process-held state handle instead of opening two connections.
  const entries = readRegisteredAgentDatabases(
    { ...options, includeIncompatibleSchemaVersions: true },
    false,
  );
  memo.entries = entries;
  const cloned = cloneRegisteredAgentDatabases(entries);
  return options.includeIncompatibleSchemaVersions
    ? cloned
    : cloned.filter((entry) => entry.schemaVersion === AETHER_AGENT_SCHEMA_VERSION);
}
