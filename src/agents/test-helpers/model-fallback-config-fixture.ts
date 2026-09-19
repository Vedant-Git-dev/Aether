/**
 * Model fallback config fixture.
 *
 * Builds a minimal config with primary and fallback models for model-selection tests.
 */
import type { AetherConfig } from "../../config/types.aether.js";

export function makeModelFallbackCfg(overrides: Partial<AetherConfig> = {}): AetherConfig {
  return {
    agents: {
      defaults: {
        model: {
          primary: "openai/gpt-4.1-mini",
          fallbacks: ["anthropic/claude-haiku-3-5"],
        },
      },
    },
    ...overrides,
  } as AetherConfig;
}

export function createModelFallbackConfig(primary: string, fallbacks: string[]): AetherConfig {
  return {
    agents: {
      defaults: {
        model: { primary, fallbacks },
      },
    },
  };
}
