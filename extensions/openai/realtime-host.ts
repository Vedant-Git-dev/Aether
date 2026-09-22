// Full registration composes the same host operations supplied to a cold capability catalog.
import { resolveAgentDir } from "aether/plugin-sdk/agent-scope-runtime";
import { formatErrorMessage } from "aether/plugin-sdk/error-runtime";
import type { PluginCapabilityCatalogContext } from "aether/plugin-sdk/plugin-entry";
import {
  isProviderAuthProfileConfigured,
  resolveProviderAuthProfileApiKey,
} from "aether/plugin-sdk/provider-auth";
import {
  createProviderHttpError,
  readProviderJsonResponse,
  readProviderTextResponse,
  resolveProviderRequestHeaders,
} from "aether/plugin-sdk/provider-http";
import {
  captureWsEvent,
  createDebugProxyWebSocketAgent,
  resolveDebugProxySettings,
} from "aether/plugin-sdk/proxy-capture";
import { createRealtimeTranscriptionWebSocketSession } from "aether/plugin-sdk/realtime-transcription-session";
import { warn } from "aether/plugin-sdk/runtime-env";
import { redactSensitiveText } from "aether/plugin-sdk/security-runtime";
import { fetchWithSsrFGuard } from "aether/plugin-sdk/ssrf-runtime";

export const openAIRealtimeHost = {
  resolveAgentDir,
  isProviderAuthProfileConfigured,
  resolveProviderAuthProfileApiKey,
  resolveProviderRequestHeaders,
  createRealtimeTranscriptionWebSocketSession,
  captureWsEvent,
  createDebugProxyWebSocketAgent,
  resolveDebugProxySettings,
  fetchWithSsrFGuard,
  createProviderHttpError,
  readProviderJsonResponse,
  readProviderTextResponse,
  formatErrorMessage,
  warn,
  redactSensitiveText,
} satisfies Omit<
  PluginCapabilityCatalogContext,
  "isProviderApiKeyConfigured" | "resolveApiKeyForProvider"
>;

export type OpenAIRealtimeHost = typeof openAIRealtimeHost;
