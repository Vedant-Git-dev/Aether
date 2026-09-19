import { executeSqliteQuerySync, getNodeSqliteKysely } from "../infra/kysely-sync.js";
import { createSubsystemLogger } from "../logging/subsystem.js";
import { tableExists } from "../state/aether-state-db-schema-helpers.js";
import type { DB as AetherStateDatabase } from "../state/aether-state-db.generated.js";
import {
  closeAetherStateDatabase,
  runAetherStateWriteTransaction,
} from "../state/aether-state-db.js";
import { upsertTaskWithDeliveryStateToSqlite } from "../tasks/task-registry.store.sqlite.js";
import type { TaskRecord } from "../tasks/task-registry.types.js";

export function clearTaskRegistrySqliteForTests(ownerKind: "task" | "flow"): void {
  try {
    runAetherStateWriteTransaction(({ db }) => {
      const kysely = getNodeSqliteKysely<AetherStateDatabase>(db);
      if (ownerKind === "task") {
        executeSqliteQuerySync(db, kysely.deleteFrom("task_delivery_state"));
        executeSqliteQuerySync(db, kysely.deleteFrom("task_runs"));
      } else {
        executeSqliteQuerySync(db, kysely.deleteFrom("flow_runs"));
      }
      // Reset selected-family orphans without enabling optional lifecycle metadata.
      if (tableExists(db, "execution_owner_lifecycle_bindings")) {
        executeSqliteQuerySync(
          db,
          kysely
            .deleteFrom("execution_owner_lifecycle_bindings")
            .where("owner_kind", "=", ownerKind),
        );
      }
    });
  } catch (error) {
    const subsystem = ownerKind === "task" ? "tasks/registry" : "tasks/task-flow-registry";
    createSubsystemLogger(subsystem).warn(`Failed to reset ${ownerKind} registry storage`, {
      error,
    });
  } finally {
    closeAetherStateDatabase();
  }
}

export function seedTaskRegistryRowsForTests(tasks: Iterable<TaskRecord>): void {
  runAetherStateWriteTransaction(() => {
    for (const task of tasks) {
      upsertTaskWithDeliveryStateToSqlite({ task });
    }
  });
}
