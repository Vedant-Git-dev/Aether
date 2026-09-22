// Aether agent database stores agent-scoped persisted runtime state.
import { existsSync } from "node:fs";
import path from "node:path";
import type { DatabaseSync } from "node:sqlite";
import { resolveStateDir } from "../config/paths.js";
import { isGatewayExternallySupervised } from "../infra/gateway-supervision.js";
import { enableNodeSqliteKyselyStatementCache } from "../infra/kysely-sync.js";
import {
  openNodeSqliteDatabase,
  supportsNodeSqliteExtensionLoading,
} from "../infra/node-sqlite.js";
import type { SqliteFileGeneration } from "../infra/sqlite-file-generation.js";
import { quarantineOrphanedSqliteSidecars } from "../infra/sqlite-files.js";
import {
  confirmSqliteFileIntegrity,
  isTerminalSqliteIntegrityError,
  runSqliteIntegrityOperationSync,
  type SqliteIntegrityDiagnostics,
  type SqliteIntegrityOperation,
  type SqliteIntegrityConfirmation,
} from "../infra/sqlite-integrity.js";
import {
  deferSqlitePostCommitPublication,
  withSqlitePostCommitPublications,
} from "../infra/sqlite-post-commit.js";
import {
  runSqliteImmediateTransactionSync,
  type SqliteTransactionOptions,
} from "../infra/sqlite-transaction.js";
import { isSqliteSchemaVersionError } from "../infra/sqlite-user-version.js";
import {
  configureSqliteConnectionPragmas,
  configureSqlitePreSchemaPragmas,
  registerSqliteCacheExitClose,
  type SqliteWalMaintenance,
} from "../infra/sqlite-wal.js";
import { normalizeAgentId } from "../routing/session-key.js";
import {
  assertAgentDeletionCleanupAliases,
  assertAgentDeletionDatabaseCleanupAccess,
  getAgentDeletionDatabaseCleanup,
  registerAgentDeletionDatabaseCleanup,
} from "./agent-deletion-cleanup.js";
import { readAgentDeletionJournal } from "./agent-deletion-journal.js";
import { createAetherAgentDatabaseAdmissionOwner } from "./aether-agent-db-admission.js";
import type {
  AetherAgentDatabase,
  AetherAgentDatabaseOptions,
} from "./aether-agent-db-contract.js";
import { registerAetherAgentDatabaseIdentity } from "./aether-agent-db-identity.js";
import {
  assertAgentDatabaseMaintenanceAccess,
  registerAgentDatabaseMaintenanceAccess,
  assertAetherAgentDatabaseLease,
  claimAetherAgentDatabaseLease,
  releaseAetherAgentDatabaseLease,
} from "./aether-agent-db-lease.js";
import {
  agentDatabaseLifecycle as cache,
  startAgentDatabaseOpenTiming,
  closeCachedAetherAgentDatabase,
  closeAetherAgentDatabaseByPath,
  closeAetherAgentDatabases,
  evictLruAgentDatabaseHandles,
  retainAgentDatabase,
  retainFailedAgentDatabaseClose,
  revokePendingAgentDatabaseOpen,
  type PendingAgentDatabaseOpen,
} from "./aether-agent-db-lifecycle.js";
import { ensureAetherAgentDatabasePermissions } from "./aether-agent-db-permissions.js";
import {
  isSameAetherAgentDatabasePath,
  registerAetherAgentDatabase,
  unregisterAetherAgentDatabase,
} from "./aether-agent-db-registry.js";
import {
  assertCanonicalAgentPersistenceVersion,
  assertExistingAgentSchemaOwner,
  assertSupportedAgentSchemaVersion,
  readExistingAgentSchemaMeta,
} from "./aether-agent-db-schema-helpers.js";
import {
  agentDatabaseIntegrityBeforeMutationSteps,
  ensureAetherAgentSchema,
} from "./aether-agent-db-schema.js";
import {
  clearAetherAgentDatabaseValidationCache,
  getValidatedAetherAgentDatabaseOwner,
  invalidateAetherAgentDatabaseValidation,
  setValidatedAetherAgentDatabaseOwner,
} from "./aether-agent-db-validation-cache.js";
import {
  isIncognitoAetherAgentSqlitePath,
  resolveAetherAgentSqlitePath,
} from "./aether-agent-db.paths.js";
import {
  clearAetherDatabaseQuarantine,
  createAetherDatabaseVerificationError,
  readAetherDatabaseQuarantine,
} from "./aether-quarantine-store.js";
import {
  AETHER_SQLITE_BUSY_TIMEOUT_MS,
  type AetherStateDatabaseOptions,
} from "./aether-state-db.js";

export {
  AETHER_AGENT_SCHEMA_VERSION,
  type AetherAgentDatabase,
  type AetherAgentDatabaseOptions,
  type AetherAgentDatabaseOwnerInspection,
  type AetherRegisteredAgentDatabase,
} from "./aether-agent-db-contract.js";
export {
  assertAetherAgentDatabaseForMaintenance,
  migrateAetherAgentDatabaseForMaintenance,
} from "./aether-agent-db-maintenance.js";
export { ensureAetherAgentDatabasePermissions } from "./aether-agent-db-permissions.js";
export {
  listAetherRegisteredAgentDatabases,
  readAetherAgentDatabaseRegistryToken,
} from "./aether-agent-db-registry.js";
export { ensureAetherAgentDatabaseSchema } from "./aether-agent-db-schema.js";
export {
  isIncognitoAetherAgentSqlitePath,
  resolveIncognitoAetherAgentSqlitePath,
  resolveAetherAgentSqlitePath,
} from "./aether-agent-db.paths.js";

export class IncognitoAgentDatabasePathCollisionError extends Error {
  readonly path: string;

  constructor(pathname: string) {
    super(
      `Incognito agent database sentinel path already exists: ${pathname}. This filename is reserved for in-memory incognito state; move or rename the file and retry.`,
    );
    this.name = "IncognitoAgentDatabasePathCollisionError";
    this.path = pathname;
  }
}

/** Reconfirm an advisory worker failure on the live owner connection. */
export function confirmAetherAgentDatabaseIntegrity(
  pathname: string,
): SqliteIntegrityConfirmation {
  const resolvedPath = path.resolve(pathname);
  closeAetherAgentDatabaseByPath(resolvedPath);
  // Closing breaks process ownership of the pathname. A replacement must
  // revalidate and claim its schema before the path can become trusted again.
  invalidateAetherAgentDatabaseValidation(resolvedPath);
  return confirmSqliteFileIntegrity(resolvedPath, resolvedPath);
}

/** Latch background verification damage so later opens fail without rescanning. */
export function recordAetherAgentDatabaseOpenFailure(
  pathname: string,
  error: Error,
  generation?: SqliteFileGeneration,
): boolean {
  const recorded = cache.terminal.record(pathname, error, generation);
  if (recorded) {
    // Quarantine revokes this process's trust because doctor may replace the file.
    invalidateAetherAgentDatabaseValidation(pathname);
  }
  return recorded;
}

/**
 * Clear a terminal open failure after doctor rewrites the database file.
 * Returns false when the persisted quarantine row survived; callers must
 * surface that, or the next open re-quarantines the repaired file.
 */
export function clearAetherAgentDatabaseOpenFailure(
  pathname: string,
  options: AetherStateDatabaseOptions = {},
): boolean {
  const resolvedPath = path.resolve(pathname);
  const cleared = clearAetherDatabaseQuarantine(resolvedPath, { env: options.env });
  cache.terminal.clear(resolvedPath);
  return cleared;
}

/** Open or return a cached per-agent database after schema and owner validation. */
export function openAetherAgentDatabase(
  options: AetherAgentDatabaseOptions,
): AetherAgentDatabase {
  return runSqliteIntegrityOperationSync(openAetherAgentDatabaseSteps(options));
}

export type { AetherAgentDatabaseWriteAdmission } from "./aether-agent-db-admission.js";
export const { withAetherAgentDatabaseAsync, withAetherAgentDatabaseAdmission } =
  createAetherAgentDatabaseAdmissionOwner(openAetherAgentDatabaseSteps);

function* openAetherAgentDatabaseSteps(
  options: AetherAgentDatabaseOptions,
  pending?: PendingAgentDatabaseOpen,
): SqliteIntegrityOperation<AetherAgentDatabase> {
  const agentId = normalizeAgentId(options.agentId);
  const databaseOptions = { ...options, agentId };
  const pathname = resolveAetherAgentSqlitePath(databaseOptions);
  getAgentDeletionDatabaseCleanup(databaseOptions)?.assertCurrent();
  const incognito = isIncognitoAetherAgentSqlitePath(pathname, databaseOptions);
  // A live successful cache entry is authoritative; failed entries remain only for disposal.
  const opened = getAetherAgentDatabaseIfOpen(databaseOptions);
  if (opened) {
    cache.databases.delete(pathname);
    cache.databases.set(pathname, opened);
    return opened;
  }
  if (!pending) {
    revokePendingAgentDatabaseOpen(pathname);
  }
  const cached = cache.databases.get(pathname);
  const allowExtension = !process.permission && supportsNodeSqliteExtensionLoading();
  if (incognito) {
    // The sentinel has no reachable durable owner, so doctor cannot safely migrate a collision.
    // Refuse operator-created state instead of silently shadowing it with volatile writes.
    if (existsSync(pathname)) {
      throw new IncognitoAgentDatabasePathCollisionError(pathname);
    }
    if (cached) {
      closeCachedAetherAgentDatabase(cached);
      cache.databases.delete(pathname);
      cache.failures.delete(pathname);
    }
    // After the collision probe, this sentinel is only a cache key: SQLite opens :memory:,
    // and no directory, lease, registry row, WAL sidecar, or file write may be created.
    const db = openNodeSqliteDatabase(":memory:", { allowExtension });
    db.enableLoadExtension(false);
    configureSqlitePreSchemaPragmas(db, {
      busyTimeoutMs: AETHER_SQLITE_BUSY_TIMEOUT_MS,
    });
    const walMaintenance = configureSqliteConnectionPragmas(db, {
      busyTimeoutMs: AETHER_SQLITE_BUSY_TIMEOUT_MS,
      databaseLabel: `aether-agent-incognito:${agentId}`,
      foreignKeys: true,
      synchronous: "NORMAL",
    });
    ensureAetherAgentSchema(db, agentId, pathname);
    registerAetherAgentDatabaseIdentity(db);
    const database = { agentId, db, path: pathname, walMaintenance };
    cache.incognito.add(database);
    cache.unregisterExitClose ??= registerSqliteCacheExitClose(closeAetherAgentDatabases);
    cache.databases.set(pathname, database);
    cache.generation += 1;
    return database;
  }
  quarantineOrphanedSqliteSidecars(pathname);
  // Latched paths are quarantined; every fresh open fails fast here until
  // doctor repairs the file and clears the latch plus the persisted row.
  const terminalFailure = cache.terminal.get(pathname);
  if (terminalFailure) {
    throw terminalFailure;
  }
  let persistedFailure: Error | undefined;
  try {
    const quarantine = readAetherDatabaseQuarantine(pathname, { env: databaseOptions.env });
    if (quarantine) {
      persistedFailure = createAetherDatabaseVerificationError(
        "agent",
        pathname,
        quarantine.reason,
      );
    }
  } catch {
    // A broken quarantine store must not brick every agent open.
    // The process latch and daily verifier still cover known damage.
  }
  if (persistedFailure) {
    recordAetherAgentDatabaseOpenFailure(pathname, persistedFailure);
    throw persistedFailure;
  }
  if (cached) {
    // A closed handle can leave Kysely and WAL helpers cached; clear both before reopening.
    closeCachedAetherAgentDatabase(cached);
    cache.databases.delete(pathname);
    cache.failures.delete(pathname);
  }
  // Lease release must retain its original state owner after ambient env changes.
  const leaseEnvironment = {
    AETHER_STATE_DIR: resolveStateDir(options.env ?? process.env),
    ...(isGatewayExternallySupervised(options.env ?? process.env)
      ? { AETHER_SUPERVISOR_MODE: "external" }
      : {}),
  };
  const leaseId = claimAetherAgentDatabaseLease({
    agentId,
    path: pathname,
    env: leaseEnvironment,
  });
  if (pending) {
    pending.assertHeld = () =>
      assertAetherAgentDatabaseLease(leaseId, {
        agentId,
        path: pathname,
        env: leaseEnvironment,
      });
  }
  const diagnostics: SqliteIntegrityDiagnostics = {};
  const finishPhase = startAgentDatabaseOpenTiming(
    agentId,
    pathname,
    pending ? "async" : "sync",
    diagnostics,
  );
  let openedDb: DatabaseSync | undefined;
  let openedDatabase: AetherAgentDatabase | undefined;
  let openedWalMaintenance: SqliteWalMaintenance | undefined;
  try {
    ensureAetherAgentDatabasePermissions(pathname, databaseOptions);
    // Free a slot before constructing the new handle: under real descriptor
    // pressure the 65th open would otherwise fail before eviction could run.
    evictLruAgentDatabaseHandles();
    // Ordinary agent state also works with SQLite builds that omit extensions.
    // Trusted borrowers may enable them only when both the runtime and permissions allow it.
    const db = openNodeSqliteDatabase(pathname, { allowExtension });
    db.enableLoadExtension(false);
    enableNodeSqliteKyselyStatementCache(db);
    openedDb = db;
    registerAetherAgentDatabaseIdentity(db);
    finishPhase("open");
    // Eviction churn must avoid migration/convergence and registry busy waits.
    // Version and owner can change while evicted, so their read-only gates run on every open.
    let isValidatedReopen = getValidatedAetherAgentDatabaseOwner(pathname) === agentId;
    const walMaintenance = yield* (function* (): SqliteIntegrityOperation<SqliteWalMaintenance> {
      let maintenance: AetherAgentDatabase["walMaintenance"] | undefined;
      try {
        db.exec(`PRAGMA busy_timeout = ${AETHER_SQLITE_BUSY_TIMEOUT_MS};`);
        assertSupportedAgentSchemaVersion(db, pathname);
        const existingSchema = readExistingAgentSchemaMeta(db);
        assertExistingAgentSchemaOwner(existingSchema, agentId, pathname);
        // Integrity is not process-stable: the file can be damaged while evicted.
        // This guard is read-only (no busy waits), so every physical open pays it.
        const requiresCurrentVersionConvergence = yield* agentDatabaseIntegrityBeforeMutationSteps(
          db,
          agentId,
          pathname,
          diagnostics,
        );
        if (isValidatedReopen && (!existingSchema || requiresCurrentVersionConvergence)) {
          // New files and same-version divergence cannot inherit an earlier validation.
          // The existing full path initializes or converges them before exposure.
          invalidateAetherAgentDatabaseValidation(pathname);
          isValidatedReopen = false;
        }
        assertCanonicalAgentPersistenceVersion(db, pathname);
        finishPhase("validation");
        configureSqlitePreSchemaPragmas(db, {
          busyTimeoutMs: AETHER_SQLITE_BUSY_TIMEOUT_MS,
        });
        maintenance = configureSqliteConnectionPragmas(db, {
          busyTimeoutMs: AETHER_SQLITE_BUSY_TIMEOUT_MS,
          databaseLabel: `aether-agent:${agentId}`,
          databasePath: pathname,
          foreignKeys: true,
          synchronous: "NORMAL",
        });
        openedWalMaintenance = maintenance;
        finishPhase("configuration");
        if (!isValidatedReopen) {
          ensureAetherAgentSchema(db, agentId, pathname);
        }
        finishPhase("schema");
        return maintenance;
      } catch (err) {
        maintenance?.close();
        if (db.isOpen) {
          db.close();
        }
        const current = cache.databases.get(pathname);
        if (!current || current.db === db) {
          invalidateAetherAgentDatabaseValidation(pathname);
        }
        if (
          err instanceof Error &&
          (isSqliteSchemaVersionError(err) || isTerminalSqliteIntegrityError(err))
        ) {
          recordAetherAgentDatabaseOpenFailure(pathname, err);
        }
        throw err;
      }
    })();
    // Concurrent admissions can fill the slot reserved before the native check.
    if (pending) {
      evictLruAgentDatabaseHandles();
    }
    ensureAetherAgentDatabasePermissions(pathname, databaseOptions);
    const database = { agentId, db, path: pathname, walMaintenance };
    openedDatabase = database;
    registerAgentDatabaseMaintenanceAccess(db);
    const cleanup = registerAgentDeletionDatabaseCleanup(database, databaseOptions);
    if (cleanup) {
      const release = retainAgentDatabase(db);
      cleanup.registerClose(() => {
        release();
        // The scope owns this connection, not a later cache entry at the same pathname.
        if (cache.databases.get(database.path) === database) {
          closeAetherAgentDatabaseByPath(database.path, database.agentId);
        } else if (database.db.isOpen) {
          throw new Error("Agent deletion cleanup lost its database close owner.");
        }
      });
    }
    if (!isValidatedReopen) {
      registerAetherAgentDatabase({ agentId, path: pathname, env: options.env });
      setValidatedAetherAgentDatabaseOwner(pathname, agentId);
    }
    cache.terminal.clear(pathname);
    // Safety net for processes that end without an orderly close: agent DBs have
    // no shutdown owner like the ACP/gateway state DB closes. Closing unregisters.
    cache.unregisterExitClose ??= registerSqliteCacheExitClose(closeAetherAgentDatabases);
    finishPhase("registration");
    cache.leases.set(pathname, { leaseId, env: leaseEnvironment });
    cache.databases.set(pathname, database);
    return database;
  } catch (error) {
    let closeError: unknown;
    if (openedDatabase) {
      try {
        closeCachedAetherAgentDatabase(openedDatabase);
      } catch (caught) {
        closeError = caught;
      }
    }
    if (openedDb?.isOpen) {
      if (
        pending &&
        cache.databases.has(pathname) &&
        cache.databases.get(pathname)?.db !== openedDb
      ) {
        // A synchronous opener may supersede pending work. Retain failed cleanup
        // with its original native owner; never overwrite the replacement cache/lease.
        const retainedDb = openedDb;
        retainFailedAgentDatabaseClose(agentId, pathname, () => {
          openedWalMaintenance?.close();
          if (retainedDb.isOpen) {
            retainedDb.close();
          }
          releaseAetherAgentDatabaseLease(leaseId, { env: leaseEnvironment });
        });
        throw error;
      }
      invalidateAetherAgentDatabaseValidation(pathname);
      const retainedDatabase =
        openedDatabase ??
        ({
          agentId,
          db: openedDb,
          path: pathname,
          walMaintenance: openedWalMaintenance ?? {
            checkpoint: () => false,
            close: () => false,
          },
        } satisfies AetherAgentDatabase);
      // Failed opens remain disposal-owned but cannot become successful cache hits.
      cache.databases.set(pathname, retainedDatabase);
      cache.leases.set(pathname, { leaseId, env: leaseEnvironment });
      cache.failures.set(pathname, closeError ?? error);
      cache.unregisterExitClose ??= registerSqliteCacheExitClose(closeAetherAgentDatabases);
    } else {
      try {
        releaseAetherAgentDatabaseLease(leaseId, { env: leaseEnvironment });
      } catch (releaseError) {
        retainFailedAgentDatabaseClose(agentId, pathname, () =>
          releaseAetherAgentDatabaseLease(leaseId, { env: leaseEnvironment }),
        );
        throw releaseError;
      }
    }
    throw closeError ?? error;
  }
}

/** Queue a non-throwing runtime publication on the outer database commit edge. */
export function deferAetherAgentPostCommitPublication(
  database: AetherAgentDatabase,
  publish: () => void,
): boolean {
  return deferSqlitePostCommitPublication(database.db, publish);
}

export function runAetherAgentWriteTransaction<T>(
  operation: (database: AetherAgentDatabase) => T,
  options: AetherAgentDatabaseOptions,
  transactionOptions: Pick<
    SqliteTransactionOptions,
    "busyTimeoutMs" | "operationLabel" | "slowTransactionHoldMs"
  > = {},
): T {
  const database = openAetherAgentDatabase(options);
  const enteredNestedTransaction = database.db.isTransaction;
  return withSqlitePostCommitPublications(database.db, () =>
    runSqliteImmediateTransactionSync(
      database.db,
      () => {
        assertAgentDeletionDatabaseCleanupAccess(database, options);
        const operationResult = operation(database);
        if (!enteredNestedTransaction && !cache.incognito.has(database)) {
          // Permission failure must roll back with the write. Repairing after
          // COMMIT could make callers retry a transaction already durable in SQLite.
          ensureAetherAgentDatabasePermissions(database.path, options);
        }
        return operationResult;
      },
      {
        busyTimeoutMs: transactionOptions.busyTimeoutMs ?? AETHER_SQLITE_BUSY_TIMEOUT_MS,
        databaseLabel: database.path,
        ...transactionOptions,
        operationLabel: transactionOptions.operationLabel ?? "agent.write",
        withCommit: getAgentDeletionDatabaseCleanup(options)?.withCommit,
      },
    ),
  );
}

/** Retain the exact verified connection across awaits; explicit disposal still revokes it. */
export function borrowAetherAgentDatabase(options: AetherAgentDatabaseOptions): {
  db: DatabaseSync;
  release: () => void;
} {
  const { db } = openAetherAgentDatabase(options);
  return { db, release: retainAgentDatabase(db) };
}

/** Return whether the exact cached agent database pathname is still open. */
export function isAetherAgentDatabaseOpen(pathname: string): boolean {
  return cache.databases.get(path.resolve(pathname))?.db.isOpen === true;
}

/** Return the matching live cache entry without materializing a database. */
export function getAetherAgentDatabaseIfOpen(
  options: AetherAgentDatabaseOptions,
): AetherAgentDatabase | undefined {
  const agentId = normalizeAgentId(options.agentId);
  const pathname = resolveAetherAgentSqlitePath({ ...options, agentId });
  // Incognito skips durable database leases, but still follows the agent deletion fence.
  if (
    isIncognitoAetherAgentSqlitePath(pathname, options) &&
    readAgentDeletionJournal(agentId, { env: options.env })
  ) {
    throw new Error(`Aether agent database is unavailable while agent ${agentId} is deleted.`);
  }
  const database = cache.databases.get(pathname);
  if (!database?.db.isOpen) {
    assertAgentDeletionCleanupAliases(options, isSameAetherAgentDatabasePath);
    return undefined;
  }
  if (cache.failures.has(pathname)) {
    throw cache.failures.get(pathname);
  }
  if (database.agentId !== agentId) {
    throw new Error(
      `Aether agent database ${pathname} is already open for agent ${database.agentId}; requested agent ${agentId}.`,
    );
  }
  assertAgentDeletionDatabaseCleanupAccess(database, options);
  assertAgentDatabaseMaintenanceAccess(database.db);
  return database;
}

/** Lists process-held incognito databases without opening new sentinel handles. */
export function listOpenIncognitoAgentDatabases(): Array<{ agentId: string; storePath: string }> {
  return [...cache.databases.values()]
    .filter((database) => database.db.isOpen && cache.incognito.has(database))
    .map((database) => ({ agentId: database.agentId, storePath: database.path }))
    .toSorted(
      (left, right) =>
        left.agentId.localeCompare(right.agentId) || left.storePath.localeCompare(right.storePath),
    );
}

/** Return the generation of process-held incognito database membership. */
export function readOpenIncognitoAgentDatabaseGeneration(): number {
  return cache.generation;
}

/** Returns whether this exact process-held database is incognito/in-memory. */
export function isIncognitoAetherAgentDatabase(database: AetherAgentDatabase): boolean {
  return cache.incognito.has(database);
}

/** List process-held agent databases without opening or inspecting fixture state. */
export function listAetherAgentDatabasesForTest(): Array<{ agentId: string; path: string }> {
  return [...cache.databases.values()]
    .filter((database) => database.db.isOpen)
    .map((database) => ({ agentId: database.agentId, path: database.path }))
    .toSorted(
      (left, right) =>
        left.agentId.localeCompare(right.agentId) || left.path.localeCompare(right.path),
    );
}

/** Close and unregister one unambiguous transient agent database by filesystem identity. */
export function disposeAetherAgentDatabaseByPath(
  pathname: string,
  options: { env?: NodeJS.ProcessEnv } = {},
): boolean {
  const resolvedPath = path.resolve(pathname);
  for (const pendingPath of cache.pending.keys()) {
    if (isSameAetherAgentDatabasePath(pendingPath, resolvedPath)) {
      revokePendingAgentDatabaseOpen(pendingPath);
    }
  }
  for (const retained of cache.retainedCloses) {
    if (isSameAetherAgentDatabasePath(retained.path, resolvedPath)) {
      retained.close();
    }
  }
  // Disposal can be followed by file deletion or recreation, so revalidate next open.
  invalidateAetherAgentDatabaseValidation(resolvedPath);
  const matchingDatabases = [...cache.databases.values()].filter((candidate) =>
    isSameAetherAgentDatabasePath(candidate.path, resolvedPath),
  );
  if (matchingDatabases.length > 1) {
    return false;
  }
  const database = matchingDatabases[0];
  if (database && cache.incognito.has(database)) {
    return closeAetherAgentDatabaseByPath(database.path);
  }
  if (!database) {
    return false;
  }
  try {
    unregisterAetherAgentDatabase({
      agentId: database.agentId,
      path: database.path,
      ...(options.env ? { env: options.env } : {}),
    });
  } finally {
    // Secret-bearing transient DBs must close even when registry maintenance
    // fails; Windows otherwise cannot remove the file during caller cleanup.
    closeAetherAgentDatabaseByPath(database.path);
  }
  return true;
}

export { withAgentDatabaseMaintenanceLease } from "./aether-agent-db-maintenance-lease.js";

/** Release fixture handles and pathname trust before a test root is recreated. */
export function closeAetherAgentDatabasesForTest(rootPath?: string): void {
  closeAetherAgentDatabases(rootPath);
  clearAetherAgentDatabaseValidationCache(rootPath);
  cache.terminal.clearAll(rootPath);
}

export {
  AETHER_AGENT_DB_OPEN_HANDLE_CAP,
  closeAetherAgentDatabaseByPath,
  closeAetherAgentDatabases,
  closeAetherAgentDatabasesAsync,
  inspectAetherAgentDatabaseOwner,
  settleAetherAgentDatabaseWorkerClose,
  type AetherAgentDatabaseWorkerCloseResult,
} from "./aether-agent-db-lifecycle.js";
