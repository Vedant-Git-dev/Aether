import { formatCliJsonFailure } from "../cli/failure-output.js";
import { exitCliAfterOutput } from "../cli/one-shot-exit.js";
import { clearNodeSqliteKyselyCacheForDatabase } from "../infra/kysely-sync.js";
import { openNodeSqliteDatabase } from "../infra/node-sqlite.js";
import { prepareSqliteReadOnlyLocation } from "../infra/sqlite-snapshot-source.js";
import { type RuntimeEnv, writeRuntimeJson } from "../runtime.js";
import { AETHER_AGENT_SCHEMA_VERSION } from "../state/aether-agent-db-contract.js";
import {
  preflightAetherDatabaseSchemas,
  type AetherDatabaseSchemaPreflight,
} from "../state/aether-database-preflight.js";
import { AETHER_STATE_SCHEMA_VERSION } from "../state/aether-state-db-contract.js";
import { tableExists } from "../state/aether-state-db-schema-helpers.js";
import { resolveAetherStateSqlitePath } from "../state/aether-state-db.paths.js";
import { readStateSchemaPublicationBlocker } from "../state/aether-state-schema-publication.js";
import { UpdateSchemaRefusalError } from "../state/aether-update-schema-refusal.js";
import { VERSION } from "../version.js";

async function readDrivingUpdater(): Promise<
  { version: string; canDeferStateSchema: boolean } | undefined
> {
  // The runtime ledger reader consults quarantine state. This diagnostic must
  // not open any live database, including a quarantine store needing recovery.
  const snapshot = await prepareSqliteReadOnlyLocation(resolveAetherStateSqlitePath(), {
    preserveSourceArtifacts: true,
  });
  try {
    const database = openNodeSqliteDatabase(snapshot.location, { readOnly: true });
    try {
      const blocker = readStateSchemaPublicationBlocker(database);
      return blocker
        ? {
            version: blocker.updaterVersion,
            canDeferStateSchema: tableExists(database, "config_machine_state"),
          }
        : undefined;
    } finally {
      clearNodeSqliteKyselyCacheForDatabase(database);
      database.close();
    }
  } finally {
    snapshot.cleanup();
  }
}

/** Refuse before CLI capture or Doctor maintenance can open writable state. */
export async function guardUpdateDoctorSchemaUpgrade(options: {
  schemas?: AetherDatabaseSchemaPreflight;
  runtime: RuntimeEnv;
  json?: boolean;
}): Promise<void> {
  if (process.env.AETHER_UPDATE_IN_PROGRESS !== "1") {
    return;
  }
  const schemas =
    options.schemas ??
    (await preflightAetherDatabaseSchemas({
      env: process.env,
      supportedVersions: {
        state: AETHER_STATE_SCHEMA_VERSION,
        agent: AETHER_AGENT_SCHEMA_VERSION,
      },
    }));
  if (!schemas.pendingMigrations?.length) {
    return;
  }
  let updater: Awaited<ReturnType<typeof readDrivingUpdater>>;
  try {
    updater = await readDrivingUpdater();
  } catch {
    // A missing or unreadable run cannot prove that the driver writes the ledger.
  }
  if (!updater) {
    return;
  }
  const blockedMigrations = schemas.pendingMigrations.filter(
    (database) => database.kind === "agent" || !updater.canDeferStateSchema,
  );
  if (blockedMigrations.length === 0) {
    return;
  }
  const error = new UpdateSchemaRefusalError(blockedMigrations, updater.version, {
    targetVersion: VERSION,
  });
  if (options.json) {
    writeRuntimeJson(options.runtime, formatCliJsonFailure(error));
    exitCliAfterOutput(options.runtime, 1);
  }
  throw error;
}
