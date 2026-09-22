// Narrow SQLite schema, path, and transaction helpers for first-party runtime.

export type { Generated, Selectable } from "kysely";

export {
  borrowAetherAgentDatabase,
  ensureAetherAgentDatabaseSchema,
  openAetherAgentDatabase,
  resolveAetherAgentSqlitePath,
} from "../state/aether-agent-db.js";
export { withAetherAgentDatabaseReadOnly } from "../state/aether-agent-db-readonly.js";
export { assertAetherAgentDatabaseForMaintenance } from "../state/aether-agent-db-maintenance.js";
export { ensureAetherAgentStandingIntentsSchema } from "../state/aether-agent-standing-intents-schema.js";
export {
  compileSqliteQueryBindings,
  executeSqliteQuerySync,
  executeSqliteQueryTakeFirstSync,
  getNodeSqliteKysely,
  iterateSqliteQuerySync,
  sqliteStringSet,
} from "../infra/kysely-sync.js";
export { openNodeSqliteDatabase } from "../infra/node-sqlite.js";
export {
  prepareSqliteReadOnlyLocation,
  prepareSqliteReadOnlyLocationSync,
} from "../infra/sqlite-snapshot-source.js";
export {
  runSqliteImmediateTransaction,
  runSqliteImmediateTransactionSync,
} from "../infra/sqlite-transaction.js";
export { tableExists } from "../state/aether-state-db-schema-helpers.js";
