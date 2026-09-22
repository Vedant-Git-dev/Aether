import type { DiscordAccountConfig, AetherConfig } from "aether/plugin-sdk/config-contracts";
// Discord helper module supports runtime config behavior.
import {
  getRuntimeConfigSourceSnapshot,
  getRuntimeConfigSnapshot,
  selectApplicableRuntimeConfig,
} from "aether/plugin-sdk/runtime-config-snapshot";

export function selectDiscordRuntimeConfig(inputConfig: AetherConfig): AetherConfig {
  return (
    selectApplicableRuntimeConfig({
      inputConfig,
      runtimeConfig: getRuntimeConfigSnapshot(),
      runtimeSourceConfig: getRuntimeConfigSourceSnapshot(),
    }) ?? inputConfig
  );
}

function withSourceActivities(
  runtimeAccount: DiscordAccountConfig | undefined,
  sourceAccount: DiscordAccountConfig | undefined,
): DiscordAccountConfig {
  const { activities: _runtimeActivities, ...runtimeRest } = runtimeAccount ?? {};
  return {
    ...runtimeRest,
    ...(sourceAccount?.activities ? { activities: sourceAccount.activities } : {}),
  };
}

/** Restores plugin-owned sensitive Activity config onto the resolved runtime shape. */
export function selectDiscordActivitiesRuntimeConfig(inputConfig: AetherConfig): AetherConfig {
  const runtimeConfig = selectDiscordRuntimeConfig(inputConfig);
  const sourceDiscord = getRuntimeConfigSourceSnapshot()?.channels?.discord;
  if (!sourceDiscord) {
    return runtimeConfig;
  }
  const runtimeDiscord = runtimeConfig.channels?.discord;
  const accountIds = new Set([
    ...Object.keys(runtimeDiscord?.accounts ?? {}),
    ...Object.keys(sourceDiscord.accounts ?? {}),
  ]);
  const accounts = Object.fromEntries(
    [...accountIds].map((accountId) => [
      accountId,
      withSourceActivities(
        runtimeDiscord?.accounts?.[accountId],
        sourceDiscord.accounts?.[accountId],
      ),
    ]),
  );
  return {
    ...runtimeConfig,
    channels: {
      ...runtimeConfig.channels,
      discord: {
        ...withSourceActivities(runtimeDiscord, sourceDiscord),
        ...(accountIds.size > 0 ? { accounts } : {}),
      },
    },
  };
}
