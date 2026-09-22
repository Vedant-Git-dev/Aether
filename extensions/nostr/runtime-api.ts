// Private runtime barrel for the bundled Nostr extension.
// Keep this barrel thin and aligned with the local extension surface.

export type { AetherConfig } from "aether/plugin-sdk/config-contracts";
export { getPluginRuntimeGatewayRequestScope } from "aether/plugin-sdk/plugin-runtime";
export type { PluginRuntime } from "aether/plugin-sdk/runtime-store";
