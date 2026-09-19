// Aether state database manages shared persisted state and migrations.
import { existsSync } from "node:fs";
import type { DatabaseSync } from "node:sqlite";
import { clearNodeSqliteKyselyCacheForDatabase } from "../infra/kysely-sync.js";
import { openNodeSqliteDatabase } from "../infra/node-sqlite.js";
import {
  normalizeSqliteNonNegativeInteger,
  readSqliteBusyTimeout,
  runWithSqliteBusyTimeout,
  setSqliteBusyTimeout,
  type SqliteLockFailureReporting,
} from "../infra/sqlite-busy-timeout.js";
import { createSqliteLifecycleAggregateError } from "../infra/sqlite-coordinator.js";
import {
  repairCanonicalSqliteIndexes,
  verifyAndRepairCanonicalSqliteIndexes,
} from "../infra/sqlite-index-schema.js";
import { assertSqliteIntegrity } from "../infra/sqlite-integrity.js";
import { assertSqliteSchemaTablesPresent } from "../infra/sqlite-schema-contract.js";
import { prepareSqliteReadOnlyLocation } from "../infra/sqlite-snapshot-source.js";
import { migrateSqliteSchemaToStrictInTransaction } from "../infra/sqlite-strict.js";
import type { SqliteTransactionOptions } from "../infra/sqlite-transaction.js";
import { readSqliteUserVersion } from "../infra/sqlite-user-version.js";
import {
  StateSchemaMutationConflictError,
  withStateSchemaFence,
} from "../infra/state-database-coordinator.js";
import { migrateLegacyCronRunLogsToTaskRuns } from "../infra/state-migrations.cron-run-logs.js";
import { createSubsystemLogger } from "../logging/subsystem.js";
import { clearAetherDatabaseQuarantine } from "./aether-quarantine-store.js";
import { repairAuditEventsSchema } from "./aether-state-db-audit-migration.js";
import {
  aetherStateDatabaseCache as stateDbCache,
  recordAetherStateDatabaseOpenFailure,
  clearAetherStateDatabaseOpenFailure,
} from "./aether-state-db-cache.js";
import {
  AETHER_DATABASE_SCHEMA_DOCS_URL,
  LAZY_ADDITIVE_STATE_TABLES,
  AETHER_SQLITE_BUSY_TIMEOUT_MS,
  AETHER_STATE_SCHEMA_VERSION,
  AETHER_STATE_STRICT_SCHEMA_VERSION,
  type AetherStateDatabase,
  type AetherStateDatabaseOptions,
} from "./aether-state-db-contract.js";
import {
  assertCurrentStateRuntimeSchema,
  isAetherStateSchemaFastPathEligible,
  needsAetherStateDatabaseSchemaRepair,
} from "./aether-state-db-fast-path.js";
import {
  assertAetherStateDatabaseForMaintenance,
  markCurrentStateSchemaVersion,
  aetherStateMigrationAssertions,
  resolveDatabasePath,
  versionedStateMigrations,
  runStateSchemaMigrationTransaction,
  writeCurrentStateSchemaMetadata,
  executeCanonicalStateSchema,
  prepareStateDatabaseSchemaRepair,
} from "./aether-state-db-maintenance.js";
import { openUnpublishedStateDatabase } from "./aether-state-db-open.js";
import * as operatorApprovalMigration from "./aether-state-db-operator-approval-migration.js";
import { ensureAetherStatePermissions } from "./aether-state-db-permissions.js";
import { withExistingAetherStateDatabaseReadOnly } from "./aether-state-db-readonly.js";
import {
  ensureAdditiveStateColumns,
  ensureFirstUseAdditiveStateColumnsForStrictMigration,
} from "./aether-state-db-schema-additive.js";
import { tableExists } from "./aether-state-db-schema-helpers.js";
import {
  type AgentDatabasePathMigrationSummary as AgentPathSummary,
  assertCanonicalStateSchemaShape,
  dropLegacyStateTables,
  migrateAgentDatabaseRelativePaths as migrateAgentPaths,
  migrateWorkerPlacementExecutionModeSchema,
  repairAgentDatabasesCompositePrimaryKey,
  repairLegacyGatewayRestartHandoffsForStrictMigration,
} from "./aether-state-db-schema-repair.js";
import { migrateSingletonStateFoldInV12 } from "./aether-state-db-schema-v12-foldin.js";
import {
  assertSupportedStateSchemaVersion,
  readStateSchemaContentVersion,
  readStateSchemaMigrationVersion,
} from "./aether-state-db-schema-version.js";
import * as sessionWatchMigration from "./aether-state-db-session-watch-migration.js";
import {
  initializeNativeAetherStateConnection,
  isUninitializedNativeStartupDatabase,
  withAetherStateStartupCheckpointConnection,
} from "./aether-state-db-startup-checkpoint.js";
import * as retirements from "./aether-state-db-table-retirements.js";
import {
  runCoordinatedStateTransaction,
  withSharedStateWriteCoordinator,
} from "./aether-state-db-write-coordination.js";
import { describeAgentPathMigration, warnAgentPathMigration } from "./aether-state-db.paths.js";
import {
  assertAetherStateWriteAllowed,
  isAetherStateWriteContentionError,
  AetherStateOwnershipError,
  runWithAetherStateWriteAccess,
} from "./aether-state-ownership.js";
import { getAetherStateRuntimeSchema } from "./aether-state-schema-compatibility.js";
import {
  readStateSchemaPublicationBlocker,
  type StateSchemaPublicationBlocker,
} from "./aether-state-schema-publication.js";
import { AETHER_STATE_SCHEMA_SQL } from "./aether-state-schema.js";
import { UpdateSchemaRefusalError } from "./aether-update-schema-refusal.js";
export { registerAetherStateDatabaseLifecycleListener } from "./aether-state-db-cache.js";

export { AETHER_DATABASE_SCHEMA_DOCS_URL, AETHER_SQLITE_BUSY_TIMEOUT_MS };
export type {
  AetherStateDatabase,
  AetherStateDatabaseOptions,
  AetherStateDatabaseSchemaMigration,
} from "./aether-state-db-contract.js";
export { assertAetherStateDatabaseForMaintenance } from "./aether-state-db-maintenance.js";
export { ensureAetherStatePermissions } from "./aether-state-db-permissions.js";
export { detectAetherStateDatabaseSchemaMigrations } from "./aether-state-db-schema-repair.js";

/** Reject a fresh shared-state open after known corruption until repair clears it. */
function assertAetherStateDatabaseFreshOpenAllowed(
  options: AetherStateDatabaseOptions = {},
): void {
  const env = options.env ?? process.env;
  stateDbCache.assertAetherStateDatabaseFreshOpenAllowedAtPath(resolveDatabasePath(options), env);
}

const stateDbLog = createSubsystemLogger("state/db");
const deferredStateDatabases = new WeakSet<DatabaseSync>();

function repairStateSchema(
  pathname: string,
  env: NodeJS.ProcessEnv,
): {
  changes: string[];
  warnings: string[];
} {
  ensureAetherStatePermissions(pathname, env);
  const db = openNodeSqliteDatabase(pathname);
  const rebuiltIndexNames = new Set<string>();
  let ownershipRefused = false;
  try {
    db.exec(`PRAGMA busy_timeout = ${AETHER_SQLITE_BUSY_TIMEOUT_MS};`);
    const repairAdmittedSchema = prepareStateDatabaseSchemaRepair(db, pathname, env);
    db.exec("PRAGMA foreign_keys = OFF;");
    const changes = runStateSchemaMigrationTransaction(
      db,
      pathname,
      () => {
        const applied = repairAdmittedSchema();
        const previousVersion = readStateSchemaMigrationVersion(db);
        if (previousVersion === AETHER_STATE_SCHEMA_VERSION) {
          for (const name of verifyAndRepairCanonicalSqliteIndexes(
            db,
            pathname,
            AETHER_STATE_SCHEMA_SQL,
            { allowMissingColumns: true },
          )) {
            rebuiltIndexNames.add(name);
          }
          // Current-schema doctor repair may normalize recognized columns or
          // table options, but it must never recreate a missing table empty.
          assertSqliteSchemaTablesPresent(db, pathname, AETHER_STATE_SCHEMA_SQL, {
            allowedMissingTables: LAZY_ADDITIVE_STATE_TABLES,
          });
        } else {
          aetherStateMigrationAssertions.get(previousVersion)?.(db, { pathname });
          assertSqliteIntegrity(db, pathname);
        }
        dropLegacyStateTables(db);
        applied.push(...retirements.runRetiredStateTableMigrations(db, previousVersion));
        if (migrateSingletonStateFoldInV12(db, previousVersion)) {
          applied.push("Folded singleton state tables into config_machine_state (v12)");
        }
        if (migrateWorkerPlacementExecutionModeSchema(db, previousVersion)) {
          applied.push("Migrated cloud worker placements to execution modes");
        }
        applied.push(
          ...describeAgentPathMigration(migrateAgentPaths(db, previousVersion, pathname)),
        );
        if (repairAgentDatabasesCompositePrimaryKey(db)) {
          applied.push(`Migrated shared state agent database registry primary key → agent_id,path`);
        }
        if (repairAuditEventsSchema(db)) {
          applied.push(
            `Migrated shared state audit event ledger → versioned message lifecycle schema`,
          );
        }
        applied.push(...operatorApprovalMigration.repairOperatorApprovalSchema(db));
        const needsSessionWatchMigration =
          sessionWatchMigration.needsSessionWatchCursorProvenanceMigration(db, previousVersion);
        const sessionWatchResult = sessionWatchMigration.migrateSessionWatchCursorProvenance(db);
        if (needsSessionWatchMigration) {
          applied.push(
            `Migrated shared state session watch cursors → provenance column (${sessionWatchResult.migratedAmbientWatches} ambient, ${sessionWatchResult.removedLegacySentinels} sentinels removed)`,
          );
        }
        assertCanonicalStateSchemaShape(db, pathname);
        if (tableExists(db, "audit_events")) {
          ensureAdditiveStateColumns(db);
          for (const migration of versionedStateMigrations) {
            if (migration.migrate(db, previousVersion)) {
              applied.push(migration.applied);
            }
          }
          executeCanonicalStateSchema(db, {
            includeVersionLazyAdditiveTables: previousVersion !== AETHER_STATE_SCHEMA_VERSION,
          });
          if (previousVersion < AETHER_STATE_STRICT_SCHEMA_VERSION) {
            repairLegacyGatewayRestartHandoffsForStrictMigration(db);
            ensureFirstUseAdditiveStateColumnsForStrictMigration(db);
          }
          const strictMigration = migrateSqliteSchemaToStrictInTransaction(
            db,
            getAetherStateRuntimeSchema({
              includeVersionLazyAdditiveTables: previousVersion !== AETHER_STATE_SCHEMA_VERSION,
            }),
            { databaseLabel: pathname },
          );
          if (strictMigration.migratedTables.length > 0) {
            applied.push(
              `Migrated shared state tables to SQLite STRICT typing (${strictMigration.migratedTables.length})`,
            );
          }
          for (const name of repairCanonicalSqliteIndexes(db, pathname, AETHER_STATE_SCHEMA_SQL, {
            verifyPhysicalIntegrity: false,
          })) {
            rebuiltIndexNames.add(name);
          }
        }
        markCurrentStateSchemaVersion(db, {
          createMetadataIfMissing: previousVersion < AETHER_STATE_SCHEMA_VERSION,
        });
        if (readStateSchemaContentVersion(db) === AETHER_STATE_SCHEMA_VERSION) {
          assertCurrentStateRuntimeSchema(db, pathname);
        }
        if (rebuiltIndexNames.size > 0) {
          applied.push(`Rebuilt canonical shared-state SQLite indexes (${rebuiltIndexNames.size})`);
        }
        return applied;
      },
      {
        busyTimeoutMs: AETHER_SQLITE_BUSY_TIMEOUT_MS,
        databaseLabel: pathname,
        operationLabel: "state.schema.repair",
      },
    );
    const quarantineCleared = clearAetherDatabaseQuarantine(pathname, { env });
    clearAetherStateDatabaseOpenFailure(pathname);
    return {
      changes,
      warnings: quarantineCleared
        ? []
        : [
            `Persisted quarantine record for ${pathname} could not be cleared; rerun aether vitals --fix so the repaired database is not refused again.`,
          ],
    };
  } catch (err) {
    if (err instanceof UpdateSchemaRefusalError) {
      throw err;
    }
    if (err instanceof AetherStateOwnershipError) {
      ownershipRefused = true;
      throw err;
    }
    // Reaching this catch inside doctor means repair itself refused or failed,
    // so the runtime asserts' "run aether vitals --fix" advice is circular here.
    const reason = String(err).replace(
      /has a legacy ([a-z ]+) schema; run aether vitals --fix to migrate it\./u,
      "has a legacy $1 schema; automatic repair refused the unrecognized schema shape.",
    );
    return {
      changes: [],
      warnings: [`Failed migrating shared state database schema at ${pathname}: ${reason}`],
    };
  } finally {
    if (db.isOpen) {
      db.exec("PRAGMA foreign_keys = ON;");
    }
    clearNodeSqliteKyselyCacheForDatabase(db);
    db.close();
    if (!ownershipRefused) {
      ensureAetherStatePermissions(pathname, env);
    }
  }
}

export function repairAetherStateDatabaseSchema(options: AetherStateDatabaseOptions = {}): {
  changes: string[];
  warnings: string[];
} {
  const env = options.env ?? process.env;
  const pathname = resolveDatabasePath(options);
  if (!existsSync(pathname)) {
    return { changes: [], warnings: [] };
  }
  return runWithAetherStateWriteAccess(
    { databasePath: pathname, env },
    "state schema repair",
    () => withStateSchemaFence({ databasePath: pathname }, () => repairStateSchema(pathname, env)),
  );
}

/** Skip the exclusive doctor repair when automatic migration sees a canonical current schema. */
export function repairAetherStateDatabaseSchemaIfNeeded(
  options: AetherStateDatabaseOptions = {},
): {
  changes: string[];
  warnings: string[];
} {
  const env = options.env ?? process.env;
  const pathname = resolveDatabasePath(options);
  if (!existsSync(pathname)) {
    return { changes: [], warnings: [] };
  }

  return runWithAetherStateWriteAccess(
    { databasePath: pathname, env },
    "state schema repair preflight/repair",
    () =>
      needsAetherStateDatabaseSchemaRepair(pathname)
        ? withStateSchemaFence({ databasePath: pathname }, () => repairStateSchema(pathname, env))
        : { changes: [], warnings: [] },
  );
}

function ensureSchema(
  db: DatabaseSync,
  pathname: string,
  env: NodeJS.ProcessEnv,
  busyTimeoutMs = AETHER_SQLITE_BUSY_TIMEOUT_MS,
  initializeNativeOnly = false,
): void {
  try {
    if (isAetherStateSchemaFastPathEligible(db, pathname)) {
      // Recheck ownership so a claim made during validation cannot retain a writable handle.
      assertAetherStateWriteAllowed({ database: db, databasePath: pathname, env });
      return;
    }
  } catch {
    // Preserve the existing transactional repair and its diagnostics for drift or corruption.
  }

  withStateSchemaFence({ databasePath: pathname }, () => {
    const now = Date.now();
    db.exec("PRAGMA foreign_keys = OFF;"); // Rebuilding referenced tables requires this before BEGIN.
    try {
      runStateSchemaMigrationTransaction(
        db,
        pathname,
        () => {
          // Recheck ownership after BEGIN IMMEDIATE to exclude a concurrent external claim.
          assertAetherStateWriteAllowed({ database: db, databasePath: pathname, env });
          assertSupportedStateSchemaVersion(db, pathname);
          // Native bootstrap admission is advisory until this transaction owns the
          // write. Never migrate state initialized or occupied by a concurrent owner.
          if (initializeNativeOnly && !isUninitializedNativeStartupDatabase(db)) {
            return [];
          }
          const previousVersion = readStateSchemaMigrationVersion(db);
          if (previousVersion === AETHER_STATE_SCHEMA_VERSION) {
            verifyAndRepairCanonicalSqliteIndexes(db, pathname, AETHER_STATE_SCHEMA_SQL, {
              allowMissingColumns: true,
              validateAfterRepair: () => assertCurrentStateRuntimeSchema(db, pathname),
            });
            ensureAdditiveStateColumns(db);
            assertCurrentStateRuntimeSchema(db, pathname);
          } else {
            aetherStateMigrationAssertions.get(previousVersion)?.(db, { pathname });
          }
          dropLegacyStateTables(db);
          const retirementMessages = retirements.runRetiredStateTableMigrations(
            db,
            previousVersion,
          );
          migrateSingletonStateFoldInV12(db, previousVersion);
          migrateWorkerPlacementExecutionModeSchema(db, previousVersion);
          const pathMigration: AgentPathSummary = migrateAgentPaths(db, previousVersion, pathname);
          ensureAdditiveStateColumns(db);
          for (const migration of versionedStateMigrations) {
            migration.migrate(db, previousVersion);
          }
          sessionWatchMigration.migrateSessionWatchCursorProvenance(db);
          assertCanonicalStateSchemaShape(db, pathname);
          executeCanonicalStateSchema(db, {
            includeVersionLazyAdditiveTables: previousVersion !== AETHER_STATE_SCHEMA_VERSION,
          });
          migrateLegacyCronRunLogsToTaskRuns(db);
          if (previousVersion < AETHER_STATE_STRICT_SCHEMA_VERSION) {
            repairLegacyGatewayRestartHandoffsForStrictMigration(db);
            ensureFirstUseAdditiveStateColumnsForStrictMigration(db);
            migrateSqliteSchemaToStrictInTransaction(
              db,
              getAetherStateRuntimeSchema({
                includeVersionLazyAdditiveTables: previousVersion !== AETHER_STATE_SCHEMA_VERSION,
              }),
              { databaseLabel: pathname },
            );
          }
          repairCanonicalSqliteIndexes(db, pathname, AETHER_STATE_SCHEMA_SQL, {
            verifyPhysicalIntegrity: false,
          });
          writeCurrentStateSchemaMetadata(db, now);
          assertAetherStateDatabaseForMaintenance(db, { pathname });
          warnAgentPathMigration(stateDbLog, pathMigration, pathname);
          return retirementMessages;
        },
        {
          busyTimeoutMs,
          databaseLabel: pathname,
          operationLabel: "state.schema.ensure",
        },
      ).forEach(retirements.logRetiredStateTableMigration);
    } finally {
      if (db.isOpen) {
        db.exec("PRAGMA foreign_keys = ON;");
      }
    }
  });
}

/** Bootstrap fresh/native-only state canonically before startup checkpoint access. */
export function withAetherStateStartupMigrationCheckpointDatabase<T>(
  callback: (db: DatabaseSync) => T,
  options: AetherStateDatabaseOptions = {},
): T {
  return withAetherStateStartupCheckpointConnection(callback, options, ensureSchema);
}

/** Complete native bootstrap without migrating mature shared state. */
export function initializeNativeAetherStateDatabase(
  options: AetherStateDatabaseOptions = {},
): void {
  initializeNativeAetherStateConnection(options, (db, pathname, env) =>
    ensureSchema(db, pathname, env, AETHER_SQLITE_BUSY_TIMEOUT_MS, true),
  );
}

/** Open existing shared state without creating, migrating, chmodding, or configuring it. */
export async function openExistingAetherStateDatabaseReadOnly(
  options: AetherStateDatabaseOptions = {},
): Promise<AetherStateDatabase | undefined> {
  const pathname = resolveDatabasePath(options);
  if (!existsSync(pathname)) {
    return undefined;
  }
  assertAetherStateDatabaseFreshOpenAllowed(options);
  const prepared = await prepareSqliteReadOnlyLocation(pathname);
  let db: DatabaseSync;
  try {
    db = openNodeSqliteDatabase(prepared.location, {
      readOnly: true,
    });
  } catch (error) {
    prepared.cleanup();
    throw error;
  }
  try {
    db.exec(`PRAGMA busy_timeout = ${AETHER_SQLITE_BUSY_TIMEOUT_MS};`);
    assertSupportedStateSchemaVersion(db, pathname);
    assertSqliteIntegrity(db, pathname);
    if (readStateSchemaContentVersion(db) === AETHER_STATE_SCHEMA_VERSION) {
      assertAetherStateDatabaseForMaintenance(db, { pathname });
    }
  } catch (error) {
    try {
      clearNodeSqliteKyselyCacheForDatabase(db);
      db.close();
    } catch {
      // Preserve the verification failure that explains why the database was refused.
    }
    prepared.cleanup();
    throw error;
  }
  let cleanupComplete = false;
  return {
    db,
    path: pathname,
    walMaintenance: {
      checkpoint: () => false,
      // Cleanup can fail transiently after the database closes. Keep the
      // close contract retryable until one call finishes both responsibilities.
      close: () => {
        const wasOpen = db.isOpen;
        if (!wasOpen && cleanupComplete) {
          return false;
        }
        try {
          if (wasOpen) {
            clearNodeSqliteKyselyCacheForDatabase(db);
            db.close();
          }
        } finally {
          cleanupComplete = prepared.cleanup();
        }
        return cleanupComplete;
      },
    },
  };
}

/** Open or return a cached shared state database after schema and migration checks. */

function openAetherStateDatabaseWithBusyTimeout(
  options: AetherStateDatabaseOptions = {},
  busyTimeoutMs = AETHER_SQLITE_BUSY_TIMEOUT_MS,
  lockFailureReporting: SqliteLockFailureReporting = "report",
): AetherStateDatabase {
  const env = options.env ?? process.env;
  if (options.database) {
    assertAetherStateWriteAllowed({
      database: options.database.db,
      databasePath: options.database.path,
      env,
    });
    return options.database;
  }
  const pathname = resolveDatabasePath(options);
  // Latched paths are quarantined: the recorder closed any live handle, and
  // every open fails fast here until doctor repairs the file and clears it.
  try {
    stateDbCache.assertAetherStateDatabaseOpenAllowed(pathname);
  } catch (error) {
    stateDbCache.recordAetherStateDatabaseLifecycleOpenError(pathname, error);
    throw error;
  }
  const cached = stateDbCache.getCachedAetherStateDatabase(pathname);
  if (cached?.db.isOpen) {
    assertAetherStateWriteAllowed({
      database: cached.db,
      databasePath: pathname,
      env,
      schemaReady: true,
    });
    if (deferredStateDatabases.has(cached.db)) {
      reconcileAetherStateSchemaPublication(options);
      if (readSqliteUserVersion(cached.db) === AETHER_STATE_SCHEMA_VERSION) {
        deferredStateDatabases.delete(cached.db);
      }
    }
    return cached;
  }
  try {
    assertAetherStateDatabaseFreshOpenAllowed(options);
  } catch (error) {
    stateDbCache.recordAetherStateDatabaseLifecycleOpenError(pathname, error);
    throw error;
  }
  let unpublished: AetherStateDatabase | undefined;
  try {
    unpublished = runWithAetherStateWriteAccess(
      { databasePath: pathname, busyTimeoutMs, env },
      "fresh state database open",
      () => {
        if (cached) {
          // A closed handle can leave Kysely and WAL helpers cached; clear both under access.
          stateDbCache.closeStaleCachedAetherStateDatabase(cached);
        }
        return (unpublished = openUnpublishedStateDatabase({
          pathname,
          env,
          busyTimeoutMs,
          lockFailureReporting,
          ensureSchema: (database) => ensureSchema(database, pathname, env, busyTimeoutMs),
          recordOpenFailure: recordAetherStateDatabaseOpenFailure,
        }));
      },
    );
  } catch (error) {
    if (lockFailureReporting === "report" || !isAetherStateWriteContentionError(error)) {
      stateDbCache.recordAetherStateDatabaseLifecycleOpenError(pathname, error);
    }
    if (!unpublished) {
      throw error;
    }
    const errors = stateDbCache.closeAetherStateDatabaseHandle(unpublished);
    if (errors.length > 0) {
      throw createSqliteLifecycleAggregateError(
        [error, ...errors],
        `Fresh Aether state database open failed releasing access and closing its unpublished handle for ${pathname}.`,
        error,
      );
    }
    throw error;
  }
  const database = stateDbCache.publishAetherStateDatabase(unpublished);
  if (readSqliteUserVersion(database.db) < AETHER_STATE_SCHEMA_VERSION) {
    deferredStateDatabases.add(database.db);
    reconcileAetherStateSchemaPublication(options);
  }
  return database;
}

/** Open or return a cached shared state database after schema and migration checks. */
export function openAetherStateDatabase(
  options: AetherStateDatabaseOptions = {},
): AetherStateDatabase {
  return openAetherStateDatabaseWithBusyTimeout(options);
}

/** The Gateway watcher also publishes without requiring a new physical database open. */
export function reconcileAetherStateSchemaPublication(
  options: AetherStateDatabaseOptions = {},
): StateSchemaPublicationBlocker | undefined {
  const pending = withExistingAetherStateDatabaseReadOnly(({ db }) => {
    if (
      readSqliteUserVersion(db) >= AETHER_STATE_SCHEMA_VERSION ||
      readStateSchemaContentVersion(db) < AETHER_STATE_SCHEMA_VERSION
    ) {
      return undefined;
    }
    return { blocker: readStateSchemaPublicationBlocker(db) };
  }, options);
  if (!pending || pending.blocker) {
    return pending?.blocker;
  }
  const pathname = resolveDatabasePath(options);
  try {
    return withStateSchemaFence({ databasePath: pathname }, () =>
      runAetherStateWriteTransaction(
        ({ db }) => {
          // The advisory read may race a new update. Re-read every driver under the write lock.
          const blocker = readStateSchemaPublicationBlocker(db);
          if (blocker) {
            return blocker;
          }
          assertAetherStateDatabaseForMaintenance(db, { pathname });
          markCurrentStateSchemaVersion(db);
          return undefined;
        },
        options,
        { operationLabel: "state.schema.publish" },
      ),
    );
  } catch (error) {
    // Current content is ready for readers; a live Gateway owns optional publication.
    if (error instanceof StateSchemaMutationConflictError) {
      return undefined;
    }
    throw error;
  }
}

/** Run one operation through the shared owner without waiting synchronously on SQLite locks. */
export function runWithAetherStateBusyTimeout<T>(
  operation: (database: AetherStateDatabase) => T,
  options: AetherStateDatabaseOptions,
  busyTimeoutMs: number,
): T {
  const normalizedTimeoutMs = normalizeSqliteNonNegativeInteger(busyTimeoutMs, "busyTimeoutMs");
  const existing = options.database ?? getAetherStateDatabaseIfOpen(options);
  if (existing) {
    return runWithSqliteBusyTimeout(existing.db, normalizedTimeoutMs, () => operation(existing), {
      lockFailureReporting: "suppress",
    });
  }
  const opened = openAetherStateDatabaseWithBusyTimeout(options, normalizedTimeoutMs, "suppress");
  try {
    return runWithSqliteBusyTimeout(opened.db, normalizedTimeoutMs, () => operation(opened), {
      lockFailureReporting: "suppress",
    });
  } finally {
    if (opened.db.isOpen) {
      setSqliteBusyTimeout(opened.db, AETHER_SQLITE_BUSY_TIMEOUT_MS);
    }
  }
}

/** Run a synchronous immediate transaction against the shared state database. */
export function runAetherStateWriteTransaction<T>(
  operation: (database: AetherStateDatabase) => T,
  options: AetherStateDatabaseOptions = {},
  transactionOptions: Pick<
    SqliteTransactionOptions,
    "busyTimeoutMs" | "operationLabel" | "slowTransactionHoldMs"
  > = {},
): T {
  const existing = options.database ?? getAetherStateDatabaseIfOpen(options);
  return withSharedStateWriteCoordinator(
    {
      databasePath: existing?.path ?? resolveDatabasePath(options),
      existing: existing?.db,
      ...transactionOptions,
    },
    () => {
      let database = existing;
      let result: T;
      try {
        const acquired = options.database
          ? openAetherStateDatabase(options)
          : (database ?? openAetherStateDatabase(options));
        database = acquired;
        result = runCoordinatedStateTransaction(
          acquired.db,
          () => {
            assertAetherStateWriteAllowed({
              database: acquired.db,
              databasePath: acquired.path,
              env: options.env ?? process.env,
              schemaReady:
                !options.database && acquired === getAetherStateDatabaseIfOpen(options),
            });
            return operation(acquired);
          },
          {
            busyTimeoutMs: transactionOptions.busyTimeoutMs ?? readSqliteBusyTimeout(acquired.db),
            databaseLabel: acquired.path,
            ...transactionOptions,
            operationLabel: transactionOptions.operationLabel ?? "state.write",
          },
        );
      } catch (error) {
        if (database) {
          stateDbCache.evictAetherStateDatabaseAfterCorruption(database, error);
        }
        throw error;
      }
      try {
        ensureAetherStatePermissions(database.path, options.env ?? process.env);
      } catch {
        // The write already committed; permission hardening is best-effort here so
        // callers never retry an operation that is durable in SQLite.
      }
      return result;
    },
  );
}

/**
 * Return a shared state handle this process already holds open, if any.
 *
 * Read-only callers use this to avoid opening a connection per call; it never
 * creates, repairs, or registers a handle.
 */
function getAetherStateDatabaseIfOpen(
  options: AetherStateDatabaseOptions = {},
): AetherStateDatabase | undefined {
  return stateDbCache.getAetherStateDatabaseIfOpenAtPath(resolveDatabasePath(options));
}

export {
  recordAetherStateDatabaseOpenFailure,
  clearAetherStateDatabaseOpenFailure,
  closeAetherStateDatabaseByPath,
  closeAetherStateDatabase,
  isAetherStateDatabaseOpen,
  closeAetherStateDatabaseForTest,
  confirmAetherStateDatabaseIntegrity,
} from "./aether-state-db-cache.js";
