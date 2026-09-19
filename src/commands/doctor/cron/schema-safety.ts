import { isSqliteSchemaVersionError } from "../../../infra/sqlite-user-version.js";
import { withExistingAetherStateDatabaseArtifactPreservingReadOnly } from "../../../state/aether-state-db-readonly.js";

export function assertCronStateSchemaSupported(env?: NodeJS.ProcessEnv): void {
  withExistingAetherStateDatabaseArtifactPreservingReadOnly(() => undefined, { env });
}

export function rethrowSqliteSchemaVersionError(error: unknown): void {
  if (isSqliteSchemaVersionError(error)) {
    throw error;
  }
}
