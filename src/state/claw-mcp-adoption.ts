import { existsSync } from "node:fs";
import { executeSqliteQuerySync, getNodeSqliteKysely } from "../infra/kysely-sync.js";
import type { DB } from "./aether-state-db.generated.js";
import {
  runAetherStateWriteTransaction,
  type AetherStateDatabaseOptions,
} from "./aether-state-db.js";
import { resolveAetherStateSqlitePath } from "./aether-state-db.paths.js";

/** Records an explicit non-Claw claim through the canonical MCP owner. */
export function markClawMcpServerIndependentlyOwned(
  name: string,
  options: AetherStateDatabaseOptions & { nowMs?: number } = {},
): number {
  const databasePath = options.path ?? resolveAetherStateSqlitePath(options.env ?? process.env);
  if (!existsSync(databasePath)) {
    return 0;
  }
  try {
    return runAetherStateWriteTransaction(({ db }) => {
      const result = executeSqliteQuerySync(
        db,
        getNodeSqliteKysely<Pick<DB, "claw_mcp_server_refs">>(db)
          .updateTable("claw_mcp_server_refs")
          .set({ independent_owner: 1, updated_at_ms: options.nowMs ?? Date.now() })
          .where("name", "=", name)
          .where("independent_owner", "!=", 1),
      );
      return Number(result.numAffectedRows);
    }, options);
  } catch {
    // The canonical MCP write already succeeded; Claw status still detects config drift.
    return 0;
  }
}
