import { StartupMaintenanceRequiredError } from "../infra/startup-maintenance-required.js";

export class AetherAgentDatabaseMediaMigrationRequiredError extends StartupMaintenanceRequiredError {
  constructor(
    readonly pathname: string,
    readonly schemaVersion: number,
  ) {
    super(
      "agent-media",
      `Aether agent database ${pathname} uses schema version ${schemaVersion}; run aether vitals --fix to migrate persisted media before using it.`,
    );
    this.name = "AetherAgentDatabaseMediaMigrationRequiredError";
  }
}
