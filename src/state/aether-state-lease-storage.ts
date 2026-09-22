import path from "node:path";
import type { DatabaseSync } from "node:sqlite";
import { runWithSqliteBusyTimeout } from "../infra/sqlite-busy-timeout.js";
import { runExistingAetherStateWriteTransaction } from "./aether-state-db-existing-write.js";
import { withAetherStateDatabaseReadOnly } from "./aether-state-db-readonly.js";
import {
  openAetherStateDatabase,
  runAetherStateWriteTransaction,
  type AetherStateDatabaseOptions,
} from "./aether-state-db.js";
import { resolveAetherStateSqlitePath } from "./aether-state-db.paths.js";
import { AETHER_STATE_SCHEMA_SQL } from "./aether-state-schema.js";

export type AetherStateLeaseDatabase = {
  scope: "shared";
  options?: AetherStateDatabaseOptions;
  /** Storage compatibility only, never authority. Acquisition still claims the real lease. */
  schemaPolicy?: "existing";
};
const leaseSchema = ["schema_meta", "state_leases"]
  .map((table) => {
    const start = AETHER_STATE_SCHEMA_SQL.indexOf(`CREATE TABLE IF NOT EXISTS ${table} (`);
    const marker = ") STRICT;";
    const end = AETHER_STATE_SCHEMA_SQL.indexOf(marker, start);
    if (start < 0 || end < 0) {
      throw new Error("Existing lease schema is unavailable.");
    }
    return AETHER_STATE_SCHEMA_SQL.slice(start, end + marker.length);
  })
  .join("\n");

export function resolveLeaseDatabasePath(database: AetherStateLeaseDatabase): string {
  return database.schemaPolicy === "existing"
    ? path.resolve(database.options?.path ?? resolveAetherStateSqlitePath(database.options?.env))
    : openAetherStateDatabase(database.options).path;
}
export function readLeaseDatabase<T>(
  database: AetherStateLeaseDatabase,
  operation: (db: DatabaseSync) => T,
): T {
  return database.schemaPolicy === "existing"
    ? withAetherStateDatabaseReadOnly(({ db }) => operation(db), database.options)
    : operation(openAetherStateDatabase(database.options).db);
}
export function withLeaseWriteTransaction<T>(
  database: AetherStateLeaseDatabase,
  operationLabel: string,
  operation: (db: DatabaseSync) => T,
  busyTimeoutMs = 0,
): T {
  if (database.schemaPolicy === "existing") {
    return runExistingAetherStateWriteTransaction(
      ({ db }) => operation(db),
      database.options ?? {},
      { operationLabel, busyTimeoutMs, schemaSql: leaseSchema },
    );
  }
  const stateDatabase = openAetherStateDatabase(database.options);
  const run = () =>
    runAetherStateWriteTransaction(
      ({ db }) => operation(db),
      { ...database.options, database: stateDatabase },
      { operationLabel, busyTimeoutMs },
    );
  return runWithSqliteBusyTimeout(stateDatabase.db, busyTimeoutMs, run);
}
