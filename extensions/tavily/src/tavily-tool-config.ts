// Tavily helper module supports tavily tool config behavior.
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import type { AetherPluginToolContext } from "aether/plugin-sdk/plugin-entry";
import type { AetherPluginApi } from "aether/plugin-sdk/plugin-runtime";

export type TavilyToolConfigContext = Pick<
  AetherPluginToolContext,
  "config" | "runtimeConfig" | "getRuntimeConfig"
>;

export function resolveTavilyToolConfig(
  api: AetherPluginApi,
  ctx?: TavilyToolConfigContext,
): AetherConfig {
  return ctx?.getRuntimeConfig?.() ?? ctx?.runtimeConfig ?? ctx?.config ?? api.config;
}
