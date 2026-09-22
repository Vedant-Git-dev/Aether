import type { DatabaseSync } from "node:sqlite";
import { clearNodeSqliteKyselyCacheForDatabase } from "../infra/kysely-sync.js";
import { openNodeSqliteDatabase } from "../infra/node-sqlite.js";
import { assertSqliteIntegrityInWorker } from "../infra/sqlite-integrity-worker.js";
import {
  createNewerSqliteSchemaVersionError,
  readSqliteUserVersion,
} from "../infra/sqlite-user-version.js";
import { normalizeAgentId } from "../routing/session-key.js";
import { AETHER_AGENT_SCHEMA_VERSION } from "./aether-agent-db-contract.js";
import { assertAgentDatabaseMaintenanceAuthority } from "./aether-agent-db-lease.js";
import {
  assertExistingAgentSchemaOwner,
  assertAetherAgentSchemaContains,
  assertSupportedAgentSchemaVersion,
  readExistingAgentSchemaMeta,
} from "./aether-agent-db-schema-helpers.js";
import { ensureAetherAgentDatabaseSchemaSteps } from "./aether-agent-db-schema.js";
import { AETHER_AGENT_SCHEMA_SQL } from "./aether-agent-schema.js";
import { AETHER_SQLITE_BUSY_TIMEOUT_MS } from "./aether-state-db.js";
import type { AetherStateLeaseContext } from "./aether-state-lease.js";

/** Require exact agent ownership without requiring the latest schema. */
export function assertAetherAgentDatabaseOwner(
  database: DatabaseSync,
  options: { agentId: string; pathname: string },
): NonNullable<ReturnType<typeof readExistingAgentSchemaMeta>> {
  const agentId = normalizeAgentId(options.agentId);
  const metadata = readExistingAgentSchemaMeta(database);
  if (!metadata) {
    throw new Error(
      `Aether agent database ${options.pathname} has no schema ownership metadata.`,
    );
  }
  assertExistingAgentSchemaOwner(metadata, agentId, options.pathname);
  if (metadata.agentId !== agentId) {
    throw new Error(
      `Aether agent database ${options.pathname} belongs to agent ${metadata.agentId}; requested agent ${agentId}.`,
    );
  }
  return metadata;
}

/** Require the exact agent owner and schema before offline file maintenance. */
export function assertAetherAgentDatabaseForMaintenance(
  database: DatabaseSync,
  options: { agentId: string; pathname: string },
): void {
  const metadata = assertAetherAgentDatabaseOwner(database, options);

  const userVersion = readSqliteUserVersion(database);
  if (userVersion > AETHER_AGENT_SCHEMA_VERSION) {
    throw createNewerSqliteSchemaVersionError(
      "Aether agent database",
      options.pathname,
      userVersion,
      AETHER_AGENT_SCHEMA_VERSION,
    );
  }
  if (userVersion !== AETHER_AGENT_SCHEMA_VERSION) {
    throw new Error(
      `Aether agent database ${options.pathname} uses schema version ${userVersion}; run aether vitals --fix before compacting it.`,
    );
  }
  if (metadata.schemaVersion !== AETHER_AGENT_SCHEMA_VERSION) {
    throw new Error(
      `Aether agent database ${options.pathname} metadata schema version ${metadata.schemaVersion ?? "invalid"} does not match ${AETHER_AGENT_SCHEMA_VERSION}; run aether vitals --fix before compacting it.`,
    );
  }
  assertAetherAgentSchemaContains(database, options.pathname, AETHER_AGENT_SCHEMA_SQL);
}

/** Upgrade or repair a supported owned schema before strict offline maintenance. */
export async function migrateAetherAgentDatabaseForMaintenance(
  options: { agentId: string; pathname: string },
  maintenance: AetherStateLeaseContext,
): Promise<void> {
  const agentId = normalizeAgentId(options.agentId);
  const pathname = options.pathname;
  const env = { ...process.env };
  const assertOwned = () => {
    maintenance.signal.throwIfAborted();
    assertAgentDatabaseMaintenanceAuthority(maintenance);
  };
  assertOwned();
  const database = openNodeSqliteDatabase(pathname);
  try {
    database.exec(`PRAGMA busy_timeout = ${AETHER_SQLITE_BUSY_TIMEOUT_MS};`);
    const metadata = readExistingAgentSchemaMeta(database);
    if (!metadata) {
      return;
    }
    assertExistingAgentSchemaOwner(metadata, agentId, pathname);
    assertSupportedAgentSchemaVersion(database, pathname);
    const userVersion = readSqliteUserVersion(database);
    const metadataVersion = metadata.schemaVersion;
    const hasCurrentVersion =
      userVersion === AETHER_AGENT_SCHEMA_VERSION &&
      metadataVersion === AETHER_AGENT_SCHEMA_VERSION;
    const hasSupportedOlderVersion =
      userVersion >= 1 &&
      userVersion < AETHER_AGENT_SCHEMA_VERSION &&
      metadataVersion !== null &&
      metadataVersion === userVersion &&
      metadataVersion >= 1 &&
      metadataVersion < AETHER_AGENT_SCHEMA_VERSION;
    if (!hasCurrentVersion && !hasSupportedOlderVersion) {
      return;
    }
    const operation = ensureAetherAgentDatabaseSchemaSteps(database, {
      agentId,
      path: pathname,
      env,
    });
    try {
      let step = operation.next();
      while (!step.done) {
        assertOwned();
        try {
          // The maintenance fence and connection survive until native Worker exit.
          // Revalidate before either resume path can repair indexes or mutate schema.
          await assertSqliteIntegrityInWorker(
            step.value.databaseLabel,
            AETHER_SQLITE_BUSY_TIMEOUT_MS,
            maintenance.signal,
          );
        } catch (error) {
          assertOwned();
          assertExistingAgentSchemaOwner(readExistingAgentSchemaMeta(database), agentId, pathname);
          assertSupportedAgentSchemaVersion(database, pathname);
          step = operation.throw(error);
          continue;
        }
        assertOwned();
        assertExistingAgentSchemaOwner(readExistingAgentSchemaMeta(database), agentId, pathname);
        assertSupportedAgentSchemaVersion(database, pathname);
        step = operation.next();
      }
    } finally {
      operation.return();
    }
    assertOwned();
    assertAetherAgentDatabaseForMaintenance(database, {
      agentId,
      pathname,
    });
  } finally {
    clearNodeSqliteKyselyCacheForDatabase(database);
    database.close();
  }
}
