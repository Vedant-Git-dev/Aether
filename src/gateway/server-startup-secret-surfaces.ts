import type { AetherConfig } from "../config/types.aether.js";
import { isTruthyEnvValue } from "../infra/env.js";

export function resolveGatewayStartupSourceConfig(
  config: AetherConfig,
  env: NodeJS.ProcessEnv,
): AetherConfig {
  const skipChannels =
    isTruthyEnvValue(env.AETHER_SKIP_CHANNELS) || isTruthyEnvValue(env.AETHER_SKIP_PROVIDERS);
  if (!skipChannels || !config.channels) {
    return config;
  }
  return {
    ...config,
    channels: undefined,
  };
}
