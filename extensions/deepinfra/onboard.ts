// Deepinfra setup module handles plugin onboarding behavior.
import {
  createAliasOnlyPresetAppliers,
  type AetherConfig,
} from "aether/plugin-sdk/provider-onboard";
import { DEEPINFRA_DEFAULT_MODEL_REF } from "./provider-static-catalog.js";

export function applyDeepInfraConfig(
  cfg: AetherConfig,
  modelRef: string = DEEPINFRA_DEFAULT_MODEL_REF,
): AetherConfig {
  return createAliasOnlyPresetAppliers({ modelRef, alias: "DeepInfra" }).applyConfig(cfg);
}
