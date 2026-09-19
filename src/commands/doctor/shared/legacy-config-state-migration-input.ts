import type { ConfigFileSnapshot } from "../../../config/types.js";
import type { AetherConfig } from "../../../config/types.aether.js";
import { migrateLegacyConfig } from "./legacy-config-migrate.js";

type StateMigrationConfigInput = {
  cfg?: AetherConfig;
  pluginDoctorConfig?: AetherConfig;
};

export function resolveStateMigrationConfigInput(params: {
  snapshot: ConfigFileSnapshot;
  baseConfig: AetherConfig;
}): StateMigrationConfigInput | null {
  const pluginDoctorConfig = (params.snapshot.sourceConfig ??
    params.snapshot.config ??
    params.snapshot.parsed) as AetherConfig | undefined;
  if (params.snapshot.valid) {
    return params.snapshot.legacyIssues.length > 0 && pluginDoctorConfig !== undefined
      ? { cfg: params.baseConfig, pluginDoctorConfig }
      : { cfg: params.baseConfig };
  }
  const migrationSource = pluginDoctorConfig ?? params.snapshot.parsed;
  if (params.snapshot.legacyIssues.length === 0 || migrationSource === undefined) {
    return null;
  }
  const migrated = migrateLegacyConfig(migrationSource);
  // Plugin config repair may retain a legacy locator until its state migration
  // completes. No config mutation must not prevent that owner from retrying.
  if (!migrated.config || migrated.partiallyValid) {
    return {
      pluginDoctorConfig: (pluginDoctorConfig ?? migrationSource) as AetherConfig,
    };
  }
  return {
    cfg: migrated.config,
    ...(pluginDoctorConfig ? { pluginDoctorConfig } : {}),
  };
}
