import type { DatabaseSync } from "node:sqlite";
import { isGatewayExternallySupervised } from "../infra/gateway-supervision.js";
import {
  clearNodeSqliteKyselyCacheForDatabase,
  executeSqliteQuerySync,
  getNodeSqliteKysely,
} from "../infra/kysely-sync.js";
import { openNodeSqliteDatabase } from "../infra/node-sqlite.js";
import { assertSqliteIntegrity } from "../infra/sqlite-integrity.js";
import { runSqliteImmediateTransactionSync } from "../infra/sqlite-transaction.js";
import { configureSqliteWalMaintenance, type SqliteWalMaintenance } from "../infra/sqlite-wal.js";
import { AETHER_SQLITE_BUSY_TIMEOUT_MS } from "./aether-state-db-contract.js";
import {
  assertAetherStateDatabaseForMaintenance,
  resolveDatabasePath,
} from "./aether-state-db-maintenance.js";
import type { DB as AetherStateKyselyDatabase } from "./aether-state-db.generated.js";
import {
  openAetherStateDatabase,
  runAetherStateWriteTransaction,
  type AetherStateDatabaseOptions,
} from "./aether-state-db.js";
import {
  inspectAetherStateOwnershipFromDatabase,
  normalizeAetherStateManagerId,
  AetherStateOwnershipMetadataError,
  STATE_SUPERVISION_KEY,
  type AetherExternalStateOwnership,
  runWithAetherStateOwnershipCoordinator,
} from "./aether-state-ownership.js";

type AetherStateOwnershipOptions = Omit<AetherStateDatabaseOptions, "database" | "readOnly">;
type OwnershipDatabase = Pick<AetherStateKyselyDatabase, "config_machine_state">;

function requireOwnershipCheckpoint(
  walMaintenance: SqliteWalMaintenance,
  databasePath: string,
): void {
  if (!walMaintenance.checkpoint()) {
    throw new Error(
      `External ownership was committed for ${databasePath}, but its WAL checkpoint failed. Retry the same ownership claim before activating the supervisor.`,
    );
  }
}

function claimOwnershipRow(
  database: DatabaseSync,
  databasePath: string,
  managerId: string,
  repairMalformed: boolean,
): AetherExternalStateOwnership {
  let current: AetherExternalStateOwnership | null = null;
  try {
    current = inspectAetherStateOwnershipFromDatabase(database, databasePath);
  } catch (error) {
    if (!repairMalformed || !(error instanceof AetherStateOwnershipMetadataError)) {
      throw error;
    }
  }
  if (current) {
    if (current.managerId !== managerId) {
      throw new Error(
        `Aether shared state is already claimed by external manager ${current.managerId}; ` +
          `manager ${managerId} cannot replace that durable ownership.`,
      );
    }
    return current;
  }
  const ownership: AetherExternalStateOwnership = {
    version: 1,
    mode: "external",
    managerId,
    claimedAt: Date.now(),
  };
  const valueJson = JSON.stringify(ownership);
  const stateDb = getNodeSqliteKysely<OwnershipDatabase>(database);
  executeSqliteQuerySync(
    database,
    stateDb
      .insertInto("config_machine_state")
      .values({
        state_key: STATE_SUPERVISION_KEY,
        value_json: valueJson,
        updated_at_ms: ownership.claimedAt,
      })
      .onConflict((conflict) =>
        conflict.column("state_key").doUpdateSet({
          value_json: valueJson,
          updated_at_ms: ownership.claimedAt,
        }),
      ),
  );
  return ownership;
}

function repairMalformedOwnershipClaim(
  databasePath: string,
  managerId: string,
): AetherExternalStateOwnership {
  return runWithAetherStateOwnershipCoordinator(
    databasePath,
    "malformed state ownership repair/checkpoint",
    () => {
      const database = openNodeSqliteDatabase(databasePath);
      let walMaintenance: SqliteWalMaintenance | undefined;
      try {
        database.exec(`PRAGMA busy_timeout = ${AETHER_SQLITE_BUSY_TIMEOUT_MS};`);
        assertSqliteIntegrity(database, databasePath);
        assertAetherStateDatabaseForMaintenance(database, { pathname: databasePath });
        walMaintenance = configureSqliteWalMaintenance(database, {
          busyTimeoutMs: AETHER_SQLITE_BUSY_TIMEOUT_MS,
          checkpointIntervalMs: 0,
          checkpointMode: "TRUNCATE",
          databaseLabel: "Aether shared state ownership",
          databasePath,
        });
        const ownership = runSqliteImmediateTransactionSync(
          database,
          () => {
            assertAetherStateDatabaseForMaintenance(database, { pathname: databasePath });
            return claimOwnershipRow(database, databasePath, managerId, true);
          },
          {
            busyTimeoutMs: AETHER_SQLITE_BUSY_TIMEOUT_MS,
            databaseLabel: databasePath,
            operationLabel: "state.ownership.repair",
          },
        );
        requireOwnershipCheckpoint(walMaintenance, databasePath);
        return ownership;
      } finally {
        walMaintenance?.close({ checkpointMode: "PASSIVE" });
        clearNodeSqliteKyselyCacheForDatabase(database);
        database.close();
      }
    },
  );
}

/** Claim durable shared-state write ownership for the active external supervisor. */
export function claimAetherStateOwnership(
  managerId: string,
  options: AetherStateOwnershipOptions = {},
): AetherExternalStateOwnership {
  const env = options.env ?? process.env;
  if (!isGatewayExternallySupervised(env)) {
    throw new Error(
      "Claiming external shared-state ownership requires AETHER_SUPERVISOR_MODE=external.",
    );
  }
  const normalizedManagerId = normalizeAetherStateManagerId(managerId);
  try {
    const database = openAetherStateDatabase(options);
    return runWithAetherStateOwnershipCoordinator(
      database.path,
      "state ownership claim/checkpoint",
      () => {
        const ownership = runAetherStateWriteTransaction(
          ({ db, path: databasePath }) =>
            claimOwnershipRow(db, databasePath, normalizedManagerId, false),
          { ...options, database },
          { operationLabel: "state.ownership.claim" },
        );
        requireOwnershipCheckpoint(database.walMaintenance, database.path);
        return ownership;
      },
    );
  } catch (error) {
    if (!(error instanceof AetherStateOwnershipMetadataError)) {
      throw error;
    }
    const ownership = repairMalformedOwnershipClaim(
      resolveDatabasePath(options),
      normalizedManagerId,
    );
    openAetherStateDatabase(options);
    return ownership;
  }
}
