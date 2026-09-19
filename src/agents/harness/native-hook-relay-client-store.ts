import path from "node:path";
import {
  clearNodeSqliteKyselyCacheForDatabase,
  executeSqliteQueryTakeFirstSync,
  getNodeSqliteKysely,
} from "../../infra/kysely-sync.js";
import { openNodeSqliteDatabase } from "../../infra/node-sqlite.js";
import { AETHER_SQLITE_BUSY_TIMEOUT_MS } from "../../state/aether-state-db-contract.js";
import { assertSupportedStateSchemaVersion } from "../../state/aether-state-db-schema-version.js";
import type { DB as AetherStateKyselyDatabase } from "../../state/aether-state-db.generated.js";
import { resolveAetherStateSqlitePath } from "../../state/aether-state-db.paths.js";
import {
  readNativeHookRelayBridgeRecordRow,
  type NativeHookRelayBridgeRecord,
} from "./native-hook-relay-bridge-record.js";

type NativeHookRelayBridgeDatabase = Pick<AetherStateKyselyDatabase, "native_hook_relay_bridges">;

/** Read one native relay locator without loading the shared-state writer lifecycle. */
export function readNativeHookRelayClientBridgeRecord(params: {
  relayId: string;
  stateDbPath?: string;
}): NativeHookRelayBridgeRecord | undefined {
  const pathname = path.resolve(params.stateDbPath ?? resolveAetherStateSqlitePath());
  const db = openNodeSqliteDatabase(pathname, { readOnly: true });
  try {
    db.exec(`PRAGMA busy_timeout = ${AETHER_SQLITE_BUSY_TIMEOUT_MS};`);
    assertSupportedStateSchemaVersion(db, pathname);
    const query = getNodeSqliteKysely<NativeHookRelayBridgeDatabase>(db)
      .selectFrom("native_hook_relay_bridges")
      .selectAll()
      .where("relay_id", "=", params.relayId);
    return readNativeHookRelayBridgeRecordRow(executeSqliteQueryTakeFirstSync(db, query));
  } finally {
    clearNodeSqliteKyselyCacheForDatabase(db);
    db.close();
  }
}
