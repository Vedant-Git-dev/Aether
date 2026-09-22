// Validating legacy config migration wrapper used by doctor config flow.
import type { LegacyConfigMigrationContext } from "../../../config/legacy.shared.js";
import type { AetherConfig } from "../../../config/types.js";
import { validateConfigObjectRawWithPlugins } from "../../../config/validation.js";
import { applyLegacyDoctorMigrations } from "./legacy-config-compat.js";

/** Apply legacy migrations and validate the resulting Aether config shape when possible. */
export function migrateLegacyConfig(
  raw: unknown,
  context?: LegacyConfigMigrationContext,
): {
  config: AetherConfig | null;
  sourceConfig?: AetherConfig;
  changes: string[];
  partiallyValid?: boolean;
} {
  const { next, changes } = applyLegacyDoctorMigrations(raw, context);
  if (!next) {
    return { config: null, changes: [] };
  }
  const resolvedCandidate = context
    ? (applyLegacyDoctorMigrations(context.resolvedRaw, context).next ?? context.resolvedRaw)
    : next;
  // Runtime defaults create unrelated plugin entries that Doctor would then load
  // and persist. Validate repair candidates without materializing those defaults.
  const validated = validateConfigObjectRawWithPlugins(resolvedCandidate);
  if (!validated.ok) {
    changes.push("Migration applied; other validation issues remain — run doctor to review.");
    return { config: next as AetherConfig, changes, partiallyValid: true };
  }
  return { config: validated.config, sourceConfig: next as AetherConfig, changes };
}
