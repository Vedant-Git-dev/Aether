import type { DatabaseSync } from "node:sqlite";
import { openNodeSqliteDatabase } from "../infra/node-sqlite.js";
import { assertSqliteIntegrity } from "../infra/sqlite-integrity.js";
import {
  collectSqliteSchemaIssues,
  createSqliteTableContractReader,
  type SqliteTableContractReader,
} from "../infra/sqlite-schema-contract.js";
import { runSqliteDeferredTransactionSync } from "../infra/sqlite-transaction.js";
import { hasLegacyCronRunLogs } from "../infra/state-migrations.cron-run-logs.js";
import { VERSION } from "../version.js";
import { AETHER_STATE_SCHEMA_VERSION } from "./aether-state-db-contract.js";
import { assertAetherStateDatabaseForMaintenance } from "./aether-state-db-maintenance.js";
import {
  assertCanonicalStateSchemaShape,
  detectAetherStateDatabaseSchemaMigrationsFromDatabase,
} from "./aether-state-db-schema-repair.js";
import {
  assertSupportedStateSchemaVersion,
  readStateSchemaMigrationVersion,
} from "./aether-state-db-schema-version.js";
import {
  getAetherStateRuntimeSchema,
  isAetherStateStartupRepairableSchemaIssue,
  STATE_PERSISTENT_SCHEMA_COMPATIBILITY,
} from "./aether-state-schema-compatibility.js";

export function needsAetherStateDatabaseSchemaRepair(pathname: string): boolean {
  let database: DatabaseSync | undefined;
  try {
    database = openNodeSqliteDatabase(pathname, { readOnly: true });
    assertSupportedStateSchemaVersion(database, pathname);
    const needsRepair =
      readStateSchemaMigrationVersion(database) !== AETHER_STATE_SCHEMA_VERSION ||
      detectAetherStateDatabaseSchemaMigrationsFromDatabase(database, pathname).length > 0;
    if (!needsRepair) {
      assertCurrentStateRuntimeSchema(database, pathname);
    }
    return needsRepair;
  } catch {
    // Preserve the repair path's existing diagnostics for unreadable or noncanonical databases.
    return true;
  } finally {
    database?.close();
  }
}

export function assertCurrentStateRuntimeSchema(
  database: DatabaseSync,
  pathname: string,
  readTable?: SqliteTableContractReader,
): void {
  assertCanonicalStateSchemaShape(database, pathname);
  assertAetherStateDatabaseForMaintenance(database, { pathname }, readTable);
}

export function isAetherStateSchemaFastPathEligible(
  database: DatabaseSync,
  pathname: string,
): boolean {
  return runSqliteDeferredTransactionSync(database, () => {
    assertSupportedStateSchemaVersion(database, pathname);
    if (readStateSchemaMigrationVersion(database) !== AETHER_STATE_SCHEMA_VERSION) {
      return false;
    }
    assertSqliteIntegrity(database, pathname);
    // Both policies see this read transaction; repair must collect fresh facts after it ends.
    const readTable = createSqliteTableContractReader(database);
    assertCurrentStateRuntimeSchema(database, pathname, readTable);
    const startupRepairRequired = collectSqliteSchemaIssues(
      database,
      getAetherStateRuntimeSchema({ includeVersionLazyAdditiveTables: false }),
      STATE_PERSISTENT_SCHEMA_COMPATIBILITY,
      readTable,
    ).some(isAetherStateStartupRepairableSchemaIssue);
    if (startupRepairRequired) {
      return false;
    }
    if (hasLegacyCronRunLogs(database)) {
      return false;
    }
    // app_version commits only after this release's repairs; same-build writes are canonical.
    const metadata = database
      .prepare("SELECT app_version FROM schema_meta WHERE meta_key = 'primary' LIMIT 1")
      .get();
    return metadata?.app_version === VERSION;
  });
}
