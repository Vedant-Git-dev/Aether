import type { DatabaseSync } from "node:sqlite";
import { runSqliteImmediateTransactionSync } from "../infra/sqlite-transaction.js";
import { AETHER_AGENT_SCHEMA_SQL } from "./aether-agent-schema.js";

export const SESSION_GOAL_OPERATIONS_TABLE = "session_goal_operations";
const ensuredDatabases = new WeakSet<DatabaseSync>();

/** First typed Goal use installs the additive receipt table, without a version bump. */
export function ensureSessionGoalOperationsSchema(db: DatabaseSync): void {
  if (ensuredDatabases.has(db)) {
    return;
  }
  const start = AETHER_AGENT_SCHEMA_SQL.indexOf(
    `CREATE TABLE IF NOT EXISTS ${SESSION_GOAL_OPERATIONS_TABLE} (`,
  );
  const end = AETHER_AGENT_SCHEMA_SQL.indexOf(
    "CREATE TABLE IF NOT EXISTS transcript_events (",
    start,
  );
  if (start < 0 || end < 0) {
    throw new Error("Aether Goal operation schema markers are missing.");
  }
  runSqliteImmediateTransactionSync(db, () => {
    db.exec(AETHER_AGENT_SCHEMA_SQL.slice(start, end)); // sqlite-allow-raw -- Canonical additive DDL only.
  });
  ensuredDatabases.add(db);
}
