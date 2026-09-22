import type { DatabaseSync } from "node:sqlite";
import {
  openAetherStateDatabase,
  runAetherStateWriteTransaction,
  type AetherStateDatabaseOptions,
} from "./aether-state-db.js";
import { AETHER_STATE_SCHEMA_SQL } from "./aether-state-schema.js";

/** Prepare canonical DDL without opening a database; each feature keeps its own handle cache. */
export function createAetherStateSchemaEnsurer(params: {
  table: string;
  endMarker?: string;
  operationLabel: string;
}): (options?: AetherStateDatabaseOptions) => void {
  const start = AETHER_STATE_SCHEMA_SQL.indexOf(
    `\nCREATE TABLE IF NOT EXISTS ${params.table} (\n`,
  );
  const endMarker = params.endMarker ?? "\n) STRICT;\n";
  const end = AETHER_STATE_SCHEMA_SQL.indexOf(endMarker, start);
  if (start < 0 || end < start) {
    throw new Error(`Canonical state schema markers are missing for ${params.table}`);
  }
  const schema = AETHER_STATE_SCHEMA_SQL.slice(start, end + endMarker.length);
  const ensuredDatabases = new WeakSet<DatabaseSync>();
  return (options = {}) => {
    const database = openAetherStateDatabase(options);
    if (ensuredDatabases.has(database.db)) {
      return;
    }
    runAetherStateWriteTransaction(
      ({ db }) => {
        db.exec(schema); // sqlite-allow-raw -- Canonical feature-local additive DDL only.
      },
      options,
      { operationLabel: params.operationLabel },
    );
    // Preserve successful wrapper-return timing, including nested savepoints.
    ensuredDatabases.add(database.db);
  };
}
