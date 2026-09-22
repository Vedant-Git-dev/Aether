import { AETHER_DATABASE_SCHEMA_DOCS_URL } from "../state/aether-state-db-contract.js";
import { resolveRuntimeServiceCommit, VERSION } from "../version.js";
import { resolveAetherPackageRootSync } from "./aether-root.js";
import { StartupMaintenanceRequiredError } from "./startup-maintenance-required.js";

type SqliteUserVersionReader = {
  prepare: (sql: string) => { get: () => unknown };
};

const SQLITE_SCHEMA_VERSION_ERROR_NAME = "SqliteSchemaVersionError";

export class SqliteSchemaVersionError extends StartupMaintenanceRequiredError {
  override name = SQLITE_SCHEMA_VERSION_ERROR_NAME;

  constructor(message: string) {
    super("newer-schema", message);
  }
}

export function isSqliteSchemaVersionError(error: unknown): error is Error {
  return (
    error instanceof SqliteSchemaVersionError ||
    (error instanceof Error && error.name === SQLITE_SCHEMA_VERSION_ERROR_NAME)
  );
}

export function readSqliteUserVersion(db: SqliteUserVersionReader): number {
  const row = db.prepare("PRAGMA user_version").get() as { user_version?: unknown } | undefined;
  return Number(row?.user_version ?? 0);
}

/**
 * Name the refusing build from immutable loaded metadata, plus its install root.
 * The path remains actionable when multiple installs share a version or build.
 */
export function describeRunningAetherBuild(): string {
  const commit = resolveRuntimeServiceCommit();
  const root = resolveAetherPackageRootSync({ moduleUrl: import.meta.url });
  const identity = commit ? `Aether ${VERSION} (${commit})` : `Aether ${VERSION}`;
  return root ? `${identity} installed at ${root}` : identity;
}

export function createNewerSqliteSchemaVersionError(
  databaseLabel: string,
  pathname: string,
  schemaVersion: number,
  supportedVersion: number,
): Error {
  return new SqliteSchemaVersionError(
    "This Aether build cannot open your existing data.\n" +
      `${databaseLabel} ${pathname} uses newer schema version ${schemaVersion}; this build supports ${supportedVersion}.\n` +
      `Refused by ${describeRunningAetherBuild()}.\n` +
      `Use a build that supports schema ${schemaVersion} or newer with this state directory. To use an older build, restore your pre-update backup created with aether backup.\n` +
      `See ${AETHER_DATABASE_SCHEMA_DOCS_URL}.`,
  );
}
