import {
  resolveStickyModelSelectionPolicy,
  type StickyModelSelectionPolicy,
} from "../../agents/sticky-model-selection.js";
import { resolveIsConfigReadOnly } from "../../config/paths.js";
import type { ModelSelectionScope } from "../../config/types.agent-defaults.js";
import type { AetherConfig } from "../../config/types.aether.js";
import { ADMIN_SCOPE } from "../operator-scopes.js";

export function resolveGatewayModelSelectionPolicy(params: {
  callerScopes: readonly string[];
  cfg: AetherConfig;
  scope?: ModelSelectionScope;
}): StickyModelSelectionPolicy {
  return resolveStickyModelSelectionPolicy({
    canPersistConfig:
      params.callerScopes.includes(ADMIN_SCOPE) && !resolveIsConfigReadOnly(process.env),
    cfg: params.cfg,
    ...(params.scope ? { scope: params.scope } : {}),
  });
}
