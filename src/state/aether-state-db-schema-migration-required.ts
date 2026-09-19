import { StartupMaintenanceRequiredError } from "../infra/startup-maintenance-required.js";

type AetherStateDatabaseSchemaMigrationRequiredKind =
  | "agent-databases-composite-primary-key"
  | "audit-events-v2"
  | "legacy-workshop-review-index";

export class AetherStateDatabaseSchemaMigrationRequiredError extends StartupMaintenanceRequiredError {
  constructor(
    override readonly kind: AetherStateDatabaseSchemaMigrationRequiredKind,
    readonly pathname: string,
  ) {
    super(
      kind,
      `Aether state database schema migration required (${kind}) at ${pathname}; run aether vitals --fix to migrate it.`,
    );
    this.name = "AetherStateDatabaseSchemaMigrationRequiredError";
  }
}
