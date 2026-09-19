// Openrouter setup module handles plugin onboarding behavior.
import {
  createAliasOnlyPresetAppliers,
  type AetherConfig,
} from "aether/plugin-sdk/provider-onboard";

export const OPENROUTER_DEFAULT_MODEL_REF = "openrouter/auto";
const openrouterPresetAppliers = createAliasOnlyPresetAppliers({
  modelRef: OPENROUTER_DEFAULT_MODEL_REF,
  alias: "OpenRouter",
});

export function applyOpenrouterProviderConfig(cfg: AetherConfig): AetherConfig {
  return openrouterPresetAppliers.applyProviderConfig(cfg);
}

export function applyOpenrouterConfig(cfg: AetherConfig): AetherConfig {
  return openrouterPresetAppliers.applyConfig(cfg);
}
