// Diagnostics Prometheus API module exposes the plugin public contract.
export type {
  DiagnosticEventMetadata,
  DiagnosticEventPayload,
} from "aether/plugin-sdk/diagnostic-runtime";
export { isInternalDiagnosticEventMetadata } from "aether/plugin-sdk/diagnostic-runtime";
export {
  emptyPluginConfigSchema,
  type AetherPluginApi,
  type AetherPluginHttpRouteHandler,
  type AetherPluginService,
  type AetherPluginServiceContext,
} from "aether/plugin-sdk/plugin-entry";
export { redactSensitiveText } from "aether/plugin-sdk/security-runtime";
