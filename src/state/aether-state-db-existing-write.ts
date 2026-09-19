import fs from "node:fs";
import path from "node:path";
import type { DatabaseSync } from "node:sqlite";
import { clearNodeSqliteKyselyCacheForDatabase } from "../infra/kysely-sync-cache-state.js";
import { executeSqliteQueryTakeFirstSync, getNodeSqliteKysely } from "../infra/kysely-sync.js";
import { setSqliteBusyTimeout } from "../infra/sqlite-busy-timeout.js";
import { assertSqliteIntegrity } from "../infra/sqlite-integrity.js";
import {
  assertSqliteSchemaContains,
  getCanonicalSqliteTableNames,
  readSqliteSchemaCookie,
} from "../infra/sqlite-schema-contract.js";
import { readSqliteUserVersion } from "../infra/sqlite-user-version.js";
import { aetherStateDatabaseCache } from "./aether-state-db-cache.js";
import {
  AETHER_SQLITE_BUSY_TIMEOUT_MS,
  type AetherStateDatabaseOptions,
} from "./aether-state-db-contract.js";
import { openTrackedStateDatabase, closeTrackedStateDatabase } from "./aether-state-db-handle.js";
import { assertAetherStateDatabaseOwner } from "./aether-state-db-maintenance.js";
import { assertSupportedStateSchemaVersion } from "./aether-state-db-schema-version.js";
import {
  runCoordinatedStateTransaction,
  withSharedStateWriteCoordinator,
} from "./aether-state-db-write-coordination.js";
import type { DB } from "./aether-state-db.generated.js";
import { resolveAetherStateSqlitePath } from "./aether-state-db.paths.js";
import {
  assertAetherStateWriteAllowed,
  runWithAetherStateWriteAccess,
} from "./aether-state-ownership.js";

/** Validate only the stable storage subset used by an existing-schema owner.
 * This read neither repairs nor grants write authority; callers retain their
 * actual handle, generation, lease and publication checks. */
function assertExistingAetherStateSchema(
  db: DatabaseSync,
  pathname: string,
  schemaSql: string,
): number {
  const version = assertSupportedStateSchemaVersion(db, pathname);
  assertAetherStateDatabaseOwner(db, { pathname });
  const metadata = executeSqliteQueryTakeFirstSync(
    db,
    getNodeSqliteKysely<Pick<DB, "schema_meta">>(db)
      .selectFrom("schema_meta")
      .select("schema_version")
      .where("meta_key", "=", "primary"),
  );
  if (version < 1 || metadata?.schema_version !== version) {
    throw new Error("Existing-state schema metadata is inconsistent.");
  }
  assertSqliteIntegrity(db, pathname);
  assertSqliteSchemaContains(db, pathname, schemaSql);
  return version;
}

/** A synchronous write to an already-compatible, caller-owned schema subset.
 * No database bootstrap, schema repair, journal-mode setup, cached publication or WAL timer.
 * First-use owners may install their declared additive tables; existing objects
 * must already match. This never opens or migrates the full runtime schema.
 * The real handle and write coordinators cover open, transaction, and close.
 */
export function runExistingAetherStateWriteTransaction<T>(
  operation: (database: { db: DatabaseSync; path: string }) => T,
  options: AetherStateDatabaseOptions,
  contract: {
    schemaSql: string;
    operationLabel: string;
    busyTimeoutMs?: number;
    initializeAdditiveSchema?: boolean;
  },
): T {
  if (options.database || options.readOnly) {
    throw new Error("Existing-state writes require their own tracked writable connection.");
  }
  const env = options.env ?? process.env;
  const busyTimeoutMs = contract.busyTimeoutMs ?? AETHER_SQLITE_BUSY_TIMEOUT_MS;
  const pathname = path.resolve(options.path ?? resolveAetherStateSqlitePath(env));
  const original = fs.lstatSync(pathname);
  if (!original.isFile()) {
    throw new Error("Existing-state write requires a regular database file.");
  }
  const assertSameFile = () => {
    const current = fs.lstatSync(pathname);
    if (!current.isFile() || current.dev !== original.dev || current.ino !== original.ino) {
      throw new Error("Existing-state database generation changed.");
    }
  };
  return withSharedStateWriteCoordinator({ databasePath: pathname, busyTimeoutMs }, () =>
    runWithAetherStateWriteAccess(
      { databasePath: pathname, env, busyTimeoutMs },
      contract.operationLabel,
      () => {
        assertSameFile();
        aetherStateDatabaseCache.assertAetherStateDatabaseFreshOpenAllowedAtPath(pathname, env);
        const db = openTrackedStateDatabase(pathname, { existingOnly: true });
        try {
          setSqliteBusyTimeout(db, busyTimeoutMs);
          return runCoordinatedStateTransaction(
            db,
            () => {
              assertSameFile();
              assertAetherStateWriteAllowed({ database: db, databasePath: pathname, env });
              const version = assertExistingAetherStateSchema(
                db,
                pathname,
                contract.initializeAdditiveSchema ? "" : contract.schemaSql,
              );
              if (contract.initializeAdditiveSchema) {
                // Validate present objects before first use: CREATE IF NOT EXISTS
                // must not hide drift or repair an incomplete existing table.
                assertSqliteSchemaContains(db, pathname, contract.schemaSql, {
                  allowedMissingTables: getCanonicalSqliteTableNames(contract.schemaSql),
                });
                db.exec(contract.schemaSql); // sqlite-allow-raw -- Declared canonical feature-local additive DDL only.
                assertSqliteSchemaContains(db, pathname, contract.schemaSql);
              }
              const schemaVersion = readSqliteSchemaCookie(db);
              const result = operation({ db, path: pathname });
              assertSameFile();
              if (
                readSqliteUserVersion(db) !== version ||
                readSqliteSchemaCookie(db) !== schemaVersion
              ) {
                throw new Error("Existing-state transaction cannot migrate schema.");
              }
              return result;
            },
            {
              busyTimeoutMs,
              databaseLabel: pathname,
              operationLabel: contract.operationLabel,
            },
          );
        } finally {
          clearNodeSqliteKyselyCacheForDatabase(db);
          closeTrackedStateDatabase(db);
        }
      },
    ),
  );
}
