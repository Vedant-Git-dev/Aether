import { AsyncLocalStorage } from "node:async_hooks";
import { statSync } from "node:fs";
import path from "node:path";
import type { DatabaseSync } from "node:sqlite";
import { clearNodeSqliteKyselyCacheForDatabase } from "../infra/kysely-sync-cache-state.js";
import { openNodeSqliteDatabase } from "../infra/node-sqlite.js";
import {
  prepareSqliteReadOnlyLocation,
  prepareSqliteReadOnlyLocationSync,
} from "../infra/sqlite-snapshot-source.js";
import { withSqliteSourceHandle } from "../infra/sqlite-source-handle.js";
import { resolveGlobalSingleton } from "../shared/global-singleton.js";
import { aetherStateDatabaseCache } from "./aether-state-db-cache.js";
import {
  AETHER_SQLITE_BUSY_TIMEOUT_MS,
  type AetherStateDatabaseOptions,
} from "./aether-state-db-contract.js";
import { openDanglingWorkshopIndexReadAdmission } from "./aether-state-db-dangling-workshop-index.js";
import { assertSupportedStateSchemaVersion } from "./aether-state-db-schema-version.js";
import { resolveAetherStateSqlitePath } from "./aether-state-db.paths.js";

const artifactPreservingReads = resolveGlobalSingleton(
  Symbol.for("aether.artifactPreservingStateReads"),
  () => new AsyncLocalStorage<boolean>(),
);

/** Admission scopes every nested reader without changing normal live-read semantics. */
export function withArtifactPreservingStateReads<T>(operation: () => T): T {
  return artifactPreservingReads.run(true, operation);
}

export function isArtifactPreservingStateRead(): boolean {
  return artifactPreservingReads.getStore() === true;
}

type AetherStateReadOnlyDatabase = {
  db: DatabaseSync;
  path: string;
};

type ReusedAetherStateReadOnlyDatabase<T> = { reused: false } | { reused: true; value: T };

/** Missing runtime tables are empty only before state grows beyond checkpoint bootstrap. */
export function hasAetherStateTablesBeyondStartupCheckpoint(db: DatabaseSync): boolean {
  return (
    /* sqlite-allow-raw -- Read-only startup-checkpoint schema discriminator. */ db
      .prepare(
        "SELECT 1 FROM main.sqlite_schema WHERE type = 'table' AND name NOT IN ('schema_meta', 'state_leases') LIMIT 1",
      )
      .get() !== undefined
  );
}

function resolveReadOnlyPath(options: AetherStateDatabaseOptions): string {
  return path.resolve(options.path ?? resolveAetherStateSqlitePath(options.env ?? process.env));
}

function existingPathOrUndefined(pathname: string): string | undefined {
  try {
    statSync(pathname);
    return pathname;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return undefined;
    }
    throw error;
  }
}

function withAetherStateDatabaseReadOnlyIfOpen<T>(
  operation: (database: AetherStateReadOnlyDatabase) => T,
  pathname: string,
): ReusedAetherStateReadOnlyDatabase<T> {
  const opened = aetherStateDatabaseCache.getAetherStateDatabaseIfOpenAtPath(pathname);
  if (!opened || opened.db.isTransaction) {
    return { reused: false };
  }
  try {
    const closeSchemaReadAdmission = openDanglingWorkshopIndexReadAdmission(opened.db);
    try {
      // Process-local terminal failures evict this handle. Persisted quarantine
      // is checked on the next physical open so hot reads do not poll metadata.
      // A newer build can migrate this file while the handle stays open, so the
      // forward-compatibility gate still runs before any reused read.
      assertSupportedStateSchemaVersion(opened.db, pathname);
      return { reused: true, value: operation(opened) };
    } finally {
      closeSchemaReadAdmission?.();
    }
  } catch (error) {
    aetherStateDatabaseCache.evictAetherStateDatabaseAfterCorruption(opened, error);
    throw error;
  }
}

function withFreshAetherStateDatabaseReadOnly<T>(
  operation: (database: AetherStateReadOnlyDatabase) => T,
  options: AetherStateDatabaseOptions,
  pathname: string,
): T {
  const env = options.env ?? process.env;
  aetherStateDatabaseCache.assertAetherStateDatabaseFreshOpenAllowedAtPath(pathname, env);
  // Even read-only SQLite opens can create a missing WAL. The existing worker
  // snapshots committed WAL pages without touching source sidecars or caller-held locks.
  const prepared = isArtifactPreservingStateRead()
    ? prepareSqliteReadOnlyLocationSync(pathname)
    : undefined;
  try {
    return withAetherStateReadOnlyLocation(operation, pathname, prepared?.location ?? pathname);
  } finally {
    prepared?.cleanup();
  }
}

function withAetherStateReadOnlyLocation<T>(
  operation: (database: AetherStateReadOnlyDatabase) => T,
  pathname: string,
  location: string,
): T {
  const read = () => {
    // node:sqlite opens without a busy handler, so a timeout installed by a
    // later PRAGMA leaves the first statement — legacy catalog admission's
    // sqlite_schema read — failing outright on any transient lock.
    const db = openNodeSqliteDatabase(location, {
      readOnly: true,
      timeout: AETHER_SQLITE_BUSY_TIMEOUT_MS,
    });
    let closeSchemaReadAdmission: (() => void) | undefined;
    try {
      closeSchemaReadAdmission = openDanglingWorkshopIndexReadAdmission(db);
      assertSupportedStateSchemaVersion(db, pathname);
      return operation({ db, path: pathname });
    } finally {
      try {
        closeSchemaReadAdmission?.();
      } finally {
        clearNodeSqliteKyselyCacheForDatabase(db);
        db.close();
      }
    }
  };
  // Only live-source descriptors join handle custody; snapshots remain private.
  return location === pathname ? withSqliteSourceHandle(pathname, read) : read();
}

/** Read shared state without joining writers; admission inherits artifact preservation. */
export function withAetherStateDatabaseReadOnly<T>(
  operation: (database: AetherStateReadOnlyDatabase) => T,
  options: AetherStateDatabaseOptions = {},
): T {
  const pathname = resolveReadOnlyPath(options);
  // Reusing a handle this process already holds keeps row loops cheap: opening
  // and closing a connection per call made shared-state reads scale with row
  // count. An in-flight transaction is skipped so callers never observe
  // uncommitted rows a fresh read-only connection could not have seen.
  const reused = withAetherStateDatabaseReadOnlyIfOpen(operation, pathname);
  if (reused.reused) {
    return reused.value;
  }
  return withFreshAetherStateDatabaseReadOnly(operation, options, pathname);
}

/** Read existing shared state while preserving non-missing filesystem failures. */
export function withExistingAetherStateDatabaseReadOnly<T>(
  operation: (database: AetherStateReadOnlyDatabase) => T,
  options: AetherStateDatabaseOptions = {},
): T | undefined {
  const pathname = resolveReadOnlyPath(options);
  const reused = withAetherStateDatabaseReadOnlyIfOpen(operation, pathname);
  if (reused.reused) {
    return reused.value;
  }
  const existingPath = existingPathOrUndefined(pathname);
  return existingPath === undefined
    ? undefined
    : withFreshAetherStateDatabaseReadOnly(operation, options, existingPath);
}

/** Read existing shared state without creating or updating its SQLite sidecars. */
export function withExistingAetherStateDatabaseArtifactPreservingReadOnly<T>(
  operation: (database: AetherStateReadOnlyDatabase) => T,
  options: AetherStateDatabaseOptions = {},
): T | undefined {
  return withArtifactPreservingStateReads(() =>
    withExistingAetherStateDatabaseReadOnly(operation, options),
  );
}

/** Preserve source artifacts while allowing the caller to progress during snapshot preparation. */
export function withExistingAetherStateDatabaseArtifactPreservingReadOnlyAsync<T>(
  operation: (database: AetherStateReadOnlyDatabase) => T,
  options: AetherStateDatabaseOptions = {},
): Promise<T | undefined> {
  return withArtifactPreservingStateReads(async () => {
    const pathname = resolveReadOnlyPath(options);
    const reused = withAetherStateDatabaseReadOnlyIfOpen(operation, pathname);
    if (reused.reused) {
      return reused.value;
    }
    if (existingPathOrUndefined(pathname) === undefined) {
      return undefined;
    }
    const env = options.env ?? process.env;
    aetherStateDatabaseCache.assertAetherStateDatabaseFreshOpenAllowedAtPath(pathname, env);
    const prepared = await prepareSqliteReadOnlyLocation(pathname, {
      preserveSourceArtifacts: true,
    });
    try {
      // Verification can quarantine the live path while the snapshot child is running.
      aetherStateDatabaseCache.assertAetherStateDatabaseFreshOpenAllowedAtPath(pathname, env);
      return withAetherStateReadOnlyLocation(operation, pathname, prepared.location);
    } finally {
      prepared.cleanup();
    }
  });
}
