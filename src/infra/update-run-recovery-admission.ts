import fs from "node:fs/promises";
import path from "node:path";
import type { AetherStateDatabaseOptions } from "../state/aether-state-db-contract.js";
import { resolveAetherStateSqlitePath } from "../state/aether-state-db.paths.js";
import { hasNodeErrorCode } from "./path-guards.js";
import { assertNoPendingUpdateRecovery } from "./update-run-recovery.js";

/** Read-only admission; neither a missing nor a replaced DB retires old recovery. */
export async function assertUpdateRecoveryAdmission(
  options: AetherStateDatabaseOptions = {},
): Promise<void> {
  const databasePath = path.resolve(
    options.path ?? resolveAetherStateSqlitePath(options.env ?? process.env),
  );
  const parent = path.dirname(databasePath);
  try {
    await fs.lstat(parent);
  } catch (error) {
    if (!hasNodeErrorCode(error, "ENOENT")) {
      throw error;
    }
    return;
  }
  // A family may hold the only original DB even when another canonical file
  // exists. Locators confer no authority to inspect, repair, or retire it.
  // Do not swallow discovery races or recreate an absent canonical database.
  const families = await fs.readdir(parent);
  if (families.some((name) => name.startsWith(".aether-restore-"))) {
    throw new Error(
      "Interrupted shared-database publication is read-only while full-state recovery is deferred",
    );
  }
  assertNoPendingUpdateRecovery(options);
}
