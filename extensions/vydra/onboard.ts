// Vydra setup module handles plugin onboarding behavior.
import type { AetherConfig } from "aether/plugin-sdk/provider-onboard";

const VYDRA_DEFAULT_IMAGE_MODEL_REF = "vydra/grok-imagine";

export function applyVydraConfig(cfg: AetherConfig): AetherConfig {
  if (cfg.agents?.defaults?.mediaModels?.image) {
    return cfg;
  }
  return {
    ...cfg,
    agents: {
      ...cfg.agents,
      defaults: {
        ...cfg.agents?.defaults,
        mediaModels: {
          ...cfg.agents?.defaults?.mediaModels,
          image: { primary: VYDRA_DEFAULT_IMAGE_MODEL_REF },
        },
      },
    },
  };
}
