// Diagnostics Otel API module exposes the plugin public contract.
export {
  createChildDiagnosticTraceContext,
  createDiagnosticTraceContext,
  emitDiagnosticEvent,
  formatDiagnosticTraceparent,
  isValidDiagnosticSpanId,
  isValidDiagnosticTraceFlags,
  isValidDiagnosticTraceId,
  onDiagnosticEvent,
  parseDiagnosticTraceparent,
  type DiagnosticEventMetadata,
  type DiagnosticEventPayload,
  type DiagnosticEventPrivateData,
  type DiagnosticTraceContext,
} from "aether/plugin-sdk/diagnostic-runtime";
export { emptyPluginConfigSchema, type AetherPluginApi } from "aether/plugin-sdk/plugin-entry";
export type {
  AetherPluginService,
  AetherPluginServiceContext,
} from "aether/plugin-sdk/plugin-entry";
export { redactSensitiveText } from "aether/plugin-sdk/security-runtime";
