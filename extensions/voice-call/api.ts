// Public voice-call API barrel exposed to plugin-local modules and tests.

export {
  definePluginEntry,
  fetchWithSsrFGuard,
  type GatewayRequestHandlerOptions,
  isBlockedHostnameOrIp,
  isRequestBodyLimitError,
  type AetherPluginApi,
  readRequestBodyWithLimit,
  requestBodyErrorToText,
  sendHttpRequestRejection,
  type SessionEntry,
  sleep,
  TtsAutoSchema,
  TtsConfigSchema,
  TtsModeSchema,
  TtsProviderSchema,
} from "./runtime-api.js";
