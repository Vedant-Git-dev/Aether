import { AsyncLocalStorage } from "node:async_hooks";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import type { DatabaseSync } from "node:sqlite";
import { hasErrnoCode } from "../infra/errno.js";
import { clearNodeSqliteKyselyCacheForDatabase } from "../infra/kysely-sync-cache-state.js";
import {
  executeSqliteQuerySync,
  executeSqliteQueryTakeFirstSync,
  getNodeSqliteKysely,
} from "../infra/kysely-sync.js";
import { openNodeSqliteDatabase } from "../infra/node-sqlite.js";
import { runWithSqliteBusyTimeout } from "../infra/sqlite-busy-timeout.js";
import { prepareStateDatabaseCanonicalMutation } from "../infra/state-database-coordinator.js";
import { normalizeAgentId } from "../routing/session-key.js";
import { resolveGlobalSingleton } from "../shared/global-singleton.js";
import { getFileLockProcessStartTime, isPidDefinitelyDead } from "../shared/pid-alive.js";
import {
  assertAgentDeletionPathFence,
  prepareAgentDeletionPathFence,
} from "./agent-deletion-journal.js";
import { aetherStateDatabaseCache } from "./aether-state-db-cache.js";
import type { AetherStateDatabaseOptions } from "./aether-state-db-contract.js";
import { openDanglingWorkshopIndexReadAdmission } from "./aether-state-db-dangling-workshop-index.js";
import { runExistingAetherStateWriteTransaction } from "./aether-state-db-existing-write.js";
import { ensureAgentDatabaseLeaseSchema } from "./aether-state-db-schema-additive.js";
import { tableExists } from "./aether-state-db-schema-helpers.js";
import type { DB as AetherStateKyselyDatabase } from "./aether-state-db.generated.js";
import {
  openAetherStateDatabase,
  runAetherStateWriteTransaction,
} from "./aether-state-db.js";
import { resolveAetherStateSqlitePath } from "./aether-state-db.paths.js";
import type { AetherStateLeaseContext } from "./aether-state-lease.js";
import { AETHER_STATE_SCHEMA_SQL } from "./aether-state-schema.js";

type AgentDatabaseLeaseDatabase = Pick<
  AetherStateKyselyDatabase,
  "agent_database_leases" | "agent_deletion_journal" | "state_leases"
>;

export const AGENT_DATABASE_MAINTENANCE_LEASE = {
  scope: "core:agent-database-maintenance",
  key: "global",
} as const;

export class AetherAgentDatabaseLeaseActiveError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AetherAgentDatabaseLeaseActiveError";
  }
}

const maintenanceAuthority = new AsyncLocalStorage<{
  authority: AetherStateLeaseContext;
  databasePath: string;
}>();

const maintenanceHandles = resolveGlobalSingleton(
  Symbol.for("aether.agentDatabaseMaintenanceHandles"),
  () => new WeakMap<DatabaseSync, () => void>(),
);

/** Keep mutation-owned cached and coalesced handles private to their exact interval. */
export function registerAgentDatabaseMaintenanceAccess(database: DatabaseSync): void {
  const owner = maintenanceAuthority.getStore();
  if (!owner) {
    return;
  }
  const assertMutation = prepareStateDatabaseCanonicalMutation(owner.databasePath);
  if (!assertMutation) {
    throw new Error("Agent database requires its live maintenance mutation scope.");
  }
  const assertCurrent = () => {
    if (maintenanceAuthority.getStore() !== owner) {
      throw new Error("Agent database belongs to another maintenance mutation scope.");
    }
    assertMutation();
    owner.authority.assertOwned();
  };
  assertCurrent();
  maintenanceHandles.set(database, assertCurrent);
}

export function assertAgentDatabaseMaintenanceAccess(database: DatabaseSync): void {
  maintenanceHandles.get(database)?.();
}

export function runWithAgentDatabaseMaintenanceAuthority<T>(
  authority: AetherStateLeaseContext,
  databasePath: string,
  run: () => Promise<T>,
): Promise<T> {
  return maintenanceAuthority.run({ authority, databasePath: path.resolve(databasePath) }, run);
}

/** Revalidate the held lease, including immediately before committing a versioned rebuild. */
export function assertAgentDatabaseMaintenanceAuthority(
  expected?: AetherStateLeaseContext,
): void {
  const authority = maintenanceAuthority.getStore()?.authority;
  if (!authority || (expected && authority !== expected)) {
    throw new Error(
      "Agent identity migration requires stopped-writer maintenance; stop active agents and run aether vitals --fix.",
    );
  }
  authority.assertOwned();
}

/** Revalidate a maintenance owner when present, without requiring ordinary opens to hold one. */
export function assertAgentDatabaseMaintenanceAuthorityIfPresent(): void {
  maintenanceAuthority.getStore()?.authority.assertOwned();
}

/** Verify the maintenance owner and its independent heartbeat before a synchronous phase. */
export function renewAgentDatabaseMaintenanceAuthorityIfPresent(): void {
  const authority = maintenanceAuthority.getStore()?.authority;
  if (!authority) {
    return;
  }
  if (!authority.renew) {
    throw new Error("Agent database maintenance authority cannot renew its lease.");
  }
  authority.renew();
}

export function claimAetherAgentDatabaseLease(
  params: { agentId: string; path: string; env?: NodeJS.ProcessEnv },
  leaseId: string = crypto.randomUUID(),
): string {
  const agentId = normalizeAgentId(params.agentId);
  const deletionFence = prepareAgentDeletionPathFence(
    { agentId, path: params.path },
    { env: params.env },
  );
  const ownerStartTime = getFileLockProcessStartTime(process.pid);
  runAetherStateWriteTransaction(
    (database) => {
      ensureAgentDatabaseLeaseSchema(database.db);
      const db = getNodeSqliteKysely<AgentDatabaseLeaseDatabase>(database.db);
      const maintenance = executeSqliteQueryTakeFirstSync(
        database.db,
        db
          .selectFrom("state_leases")
          .select("owner")
          .where("scope", "=", AGENT_DATABASE_MAINTENANCE_LEASE.scope)
          .where("lease_key", "=", AGENT_DATABASE_MAINTENANCE_LEASE.key)
          .where("expires_at", ">", Date.now()),
      );
      const authority = maintenanceAuthority.getStore();
      if (maintenance || authority) {
        // The updater's Doctor may use normal agent stores only inside its own
        // live canonical-mutation scope. Plain maintenance and foreign tasks
        // remain excluded; neither a saved owner nor a missing/expired row grants access.
        if (
          !authority ||
          authority.databasePath !== path.resolve(database.path) ||
          !prepareStateDatabaseCanonicalMutation(database.path)
        ) {
          throw new Error(
            "Agent database maintenance is in progress; retry after aether vitals --fix completes.",
          );
        }
        authority.authority.assertOwnedInTransaction(database.db);
      }
      assertAgentDeletionPathFence(database, deletionFence);
      executeSqliteQuerySync(
        database.db,
        db.insertInto("agent_database_leases").values({
          lease_id: leaseId,
          agent_id: agentId,
          path: params.path,
          owner_pid: process.pid,
          owner_start_time: ownerStartTime,
          opened_at: Date.now(),
        }),
      );
    },
    { env: params.env },
  );
  return leaseId;
}

export function releaseAetherAgentDatabaseLease(
  leaseId: string,
  options: AetherStateDatabaseOptions = {},
): void {
  const maintenance = maintenanceAuthority.getStore();
  const databasePath = path.resolve(
    options.database?.path ?? options.path ?? resolveAetherStateSqlitePath(options.env),
  );
  if (maintenance?.databasePath === databasePath) {
    return withExistingAgentLeaseWrite(maintenance.authority, options, (database) => {
      const db = getNodeSqliteKysely<AgentDatabaseLeaseDatabase>(database);
      executeSqliteQuerySync(
        database,
        db.deleteFrom("agent_database_leases").where("lease_id", "=", leaseId),
      );
    });
  }
  runAetherStateWriteTransaction((database) => {
    ensureAgentDatabaseLeaseSchema(database.db);
    const db = getNodeSqliteKysely<AgentDatabaseLeaseDatabase>(database.db);
    executeSqliteQuerySync(
      database.db,
      db.deleteFrom("agent_database_leases").where("lease_id", "=", leaseId),
    );
  }, options);
}

/** An awaited open may consume its scan only while its original runtime claim survives. */
export function assertAetherAgentDatabaseLease(
  leaseId: string,
  params: { agentId: string; path: string; env?: NodeJS.ProcessEnv },
): void {
  const ownerStartTime = getFileLockProcessStartTime(process.pid);
  const database = openAetherStateDatabase({ env: params.env });
  const db = getNodeSqliteKysely<AgentDatabaseLeaseDatabase>(database.db);
  const held = executeSqliteQueryTakeFirstSync(
    database.db,
    db
      .selectFrom("agent_database_leases")
      .select(["agent_id", "path", "owner_pid", "owner_start_time"])
      .where("lease_id", "=", leaseId),
  );
  if (
    !held ||
    held.agent_id !== params.agentId ||
    held.path !== params.path ||
    held.owner_pid !== process.pid ||
    // Claims allow an unavailable start identity; only two known identities prove reuse.
    (held.owner_start_time !== null &&
      ownerStartTime !== null &&
      held.owner_start_time !== ownerStartTime)
  ) {
    throw new Error(`Agent database open lost its runtime lease: ${params.path}`);
  }
}

function readAgentDatabaseLeases(database: DatabaseSync) {
  const db = getNodeSqliteKysely<AgentDatabaseLeaseDatabase>(database);
  return executeSqliteQuerySync(
    database,
    db
      .selectFrom("agent_database_leases")
      .select(["agent_id", "lease_id", "owner_pid", "owner_start_time", "path"]),
  ).rows;
}

function isAgentDatabaseLeaseStale(row: {
  owner_pid: number;
  owner_start_time: number | null;
}): boolean {
  if (isPidDefinitelyDead(row.owner_pid)) {
    return true;
  }
  const currentStartTime = getFileLockProcessStartTime(row.owner_pid);
  return (
    row.owner_start_time !== null &&
    currentStartTime !== null &&
    row.owner_start_time !== currentStartTime
  );
}

/** Doctor holds both lifecycle coordinators before checking writers, without schema repair. */
export function assertNoAetherAgentDatabaseLeasesReadOnly(
  options: AetherStateDatabaseOptions = {},
): void {
  const pathname = path.resolve(options.path ?? resolveAetherStateSqlitePath(options.env));
  try {
    fs.statSync(pathname);
  } catch (error) {
    if (hasErrnoCode(error, "ENOENT")) {
      return;
    }
    throw error;
  }
  // Admission must also work after restoring a quarantined database. Runtime
  // readers reject that receipt before Doctor can verify and clear it.
  const cached = aetherStateDatabaseCache.isAetherStateDatabaseOpen(pathname)
    ? aetherStateDatabaseCache.getAetherStateDatabaseIfOpenAtPath(pathname)
    : undefined;
  const db = cached?.db ?? openNodeSqliteDatabase(pathname, { readOnly: true });
  let closeSchemaReadAdmission: (() => void) | undefined;
  try {
    closeSchemaReadAdmission = openDanglingWorkshopIndexReadAdmission(db);
    runWithSqliteBusyTimeout(db, 250, () => {
      if (!tableExists(db, "agent_database_leases")) {
        return;
      }
      const owner = readAgentDatabaseLeases(db).find((row) => !isAgentDatabaseLeaseStale(row));
      if (owner) {
        throw new AetherAgentDatabaseLeaseActiveError(
          `Agent ${owner.agent_id} database is still open in process ${owner.owner_pid}; stop that process before Doctor repair.`,
        );
      }
    });
  } finally {
    try {
      closeSchemaReadAdmission?.();
    } finally {
      if (!cached) {
        clearNodeSqliteKyselyCacheForDatabase(db);
        db.close();
      }
    }
  }
}

export function assertNoAetherAgentDatabaseLeases(
  agentIdRaw: string | AetherStateLeaseContext,
  options: AetherStateDatabaseOptions & { schemaPolicy?: "existing" } = {},
): void {
  if (options.schemaPolicy === "existing") {
    if (typeof agentIdRaw === "string") {
      throw new Error("Existing-schema agent drainage requires a real maintenance owner.");
    }
    return assertNoExistingAgentDatabaseLeases(agentIdRaw, options);
  }
  const maintenance = typeof agentIdRaw === "string" ? undefined : agentIdRaw;
  const agentId = typeof agentIdRaw === "string" ? normalizeAgentId(agentIdRaw) : undefined;
  const rows = runAetherStateWriteTransaction((database) => {
    maintenance?.assertOwnedInTransaction(database.db);
    ensureAgentDatabaseLeaseSchema(database.db);
    return readAgentDatabaseLeases(database.db);
  }, options);

  const staleLeaseIds = rows.filter(isAgentDatabaseLeaseStale).map((row) => row.lease_id);
  if (staleLeaseIds.length > 0) {
    runAetherStateWriteTransaction((database) => {
      maintenance?.assertOwnedInTransaction(database.db);
      ensureAgentDatabaseLeaseSchema(database.db);
      const db = getNodeSqliteKysely<AgentDatabaseLeaseDatabase>(database.db);
      executeSqliteQuerySync(
        database.db,
        db.deleteFrom("agent_database_leases").where("lease_id", "in", staleLeaseIds),
      );
    }, options);
  }
  const staleLeaseIdSet = new Set(staleLeaseIds);
  for (const row of rows) {
    if (staleLeaseIdSet.has(row.lease_id)) {
      continue;
    }
    const deletionFence = agentId
      ? prepareAgentDeletionPathFence(
          { agentId: row.agent_id, path: row.path, fenceAgentId: agentId },
          options,
        )
      : undefined;
    let leaseStillExists = false;
    runAetherStateWriteTransaction((database) => {
      maintenance?.assertOwnedInTransaction(database.db);
      ensureAgentDatabaseLeaseSchema(database.db);
      const db = getNodeSqliteKysely<AgentDatabaseLeaseDatabase>(database.db);
      leaseStillExists =
        executeSqliteQueryTakeFirstSync(
          database.db,
          db
            .selectFrom("agent_database_leases")
            .select("lease_id")
            .where("lease_id", "=", row.lease_id),
        ) !== undefined;
      if (leaseStillExists && row.agent_id !== agentId && deletionFence) {
        assertAgentDeletionPathFence(database, deletionFence);
      }
    }, options);
    if (leaseStillExists && (!agentId || row.agent_id === agentId)) {
      const remediation = agentId ? "." : "; stop that process and rerun aether vitals --fix.";
      throw new AetherAgentDatabaseLeaseActiveError(
        `Agent ${row.agent_id} database is still open in another process${remediation}`,
      );
    }
  }
}

const existingAgentLeaseSchema = ["schema_meta", "state_leases", "agent_database_leases"]
  .map((table) => {
    const start = AETHER_STATE_SCHEMA_SQL.indexOf(`CREATE TABLE IF NOT EXISTS ${table} (`);
    const end = AETHER_STATE_SCHEMA_SQL.indexOf(") STRICT;", start);
    if (start < 0 || end < 0) {
      throw new Error("Existing agent lease schema is unavailable.");
    }
    return AETHER_STATE_SCHEMA_SQL.slice(start, end + ") STRICT;".length);
  })
  .join("\n");

function withExistingAgentLeaseWrite<T>(
  maintenance: AetherStateLeaseContext,
  options: AetherStateDatabaseOptions,
  operation: (db: DatabaseSync) => T,
): T {
  return runExistingAetherStateWriteTransaction(
    ({ db }) => {
      maintenance.assertOwnedInTransaction(db);
      const result = operation(db);
      maintenance.assertOwnedInTransaction(db);
      return result;
    },
    options,
    {
      operationLabel: "agent.database.maintenance.admission",
      schemaSql: existingAgentLeaseSchema,
      busyTimeoutMs: 0,
    },
  );
}

/** Stable existing rows can be drained before the candidate is allowed to migrate. */
function assertNoExistingAgentDatabaseLeases(
  maintenance: AetherStateLeaseContext,
  options: AetherStateDatabaseOptions,
): void {
  withExistingAgentLeaseWrite(maintenance, options, (db) => {
    const query = getNodeSqliteKysely<AgentDatabaseLeaseDatabase>(db);
    const rows = executeSqliteQuerySync(
      db,
      query
        .selectFrom("agent_database_leases")
        .select(["agent_id", "lease_id", "owner_pid", "owner_start_time"]),
    ).rows;
    for (const row of rows) {
      const currentStart = getFileLockProcessStartTime(row.owner_pid);
      if (
        isPidDefinitelyDead(row.owner_pid) ||
        (row.owner_start_time !== null &&
          currentStart !== null &&
          row.owner_start_time !== currentStart)
      ) {
        executeSqliteQuerySync(
          db,
          query.deleteFrom("agent_database_leases").where("lease_id", "=", row.lease_id),
        );
      } else {
        throw new AetherAgentDatabaseLeaseActiveError(
          `Agent ${row.agent_id} database is still open in another process; stop that process and retry.`,
        );
      }
    }
  });
}
