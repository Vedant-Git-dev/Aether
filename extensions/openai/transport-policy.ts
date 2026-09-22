// Openai plugin module implements transport policy behavior.
import type {
  ProviderResolveTransportTurnStateContext,
  ProviderTransportTurnState,
} from "aether/plugin-sdk/plugin-entry";
import { normalizeProviderId } from "aether/plugin-sdk/provider-model-metadata";
import { normalizeLowercaseStringOrEmpty } from "aether/plugin-sdk/string-coerce-runtime";
import { isOpenAIApiBaseUrl, isOpenAICodexBaseUrl } from "./base-url.js";

const DEFAULT_OPENAI_WS_DEGRADE_COOLDOWN_MS = 60_000;
const AZURE_PROVIDER_IDS = new Set(["azure-openai", "azure-openai-responses"]);

function isAzureOpenAIBaseUrl(baseUrl?: string): boolean {
  const trimmed = baseUrl?.trim();
  if (!trimmed) {
    return false;
  }
  try {
    return normalizeLowercaseStringOrEmpty(new URL(trimmed).hostname).endsWith(".openai.azure.com");
  } catch {
    return false;
  }
}

function normalizeIdentityValue(value: string, maxLength = 160): string {
  const trimmed = value.trim().replace(/[\r\n]+/g, " ");
  return trimmed.length > maxLength ? trimmed.slice(0, maxLength) : trimmed;
}

function usesKnownNativeOpenAIRoute(provider: string, baseUrl?: string): boolean {
  const normalizedProvider = normalizeProviderId(provider);
  if (!normalizedProvider) {
    return false;
  }
  if (normalizedProvider === "openai") {
    return !baseUrl || isOpenAIApiBaseUrl(baseUrl) || isOpenAICodexBaseUrl(baseUrl);
  }
  if (AZURE_PROVIDER_IDS.has(normalizedProvider)) {
    return !baseUrl || isAzureOpenAIBaseUrl(baseUrl);
  }
  return false;
}

function resolveSessionHeaders(sessionIdValue?: string): Record<string, string> | undefined {
  if (!sessionIdValue) {
    return undefined;
  }
  const sessionId = normalizeIdentityValue(sessionIdValue);
  if (!sessionId) {
    return undefined;
  }
  return {
    "x-client-request-id": sessionId,
    "x-aether-session-id": sessionId,
  };
}

export function resolveOpenAITransportTurnState(
  ctx: ProviderResolveTransportTurnStateContext,
): ProviderTransportTurnState | undefined {
  if (!usesKnownNativeOpenAIRoute(ctx.provider, ctx.model?.baseUrl)) {
    return undefined;
  }
  const sessionHeaders = resolveSessionHeaders(ctx.sessionId);
  if (!sessionHeaders) {
    return ctx.transport === "websocket"
      ? { websocket: { degradeCooldownMs: DEFAULT_OPENAI_WS_DEGRADE_COOLDOWN_MS } }
      : undefined;
  }

  const turnId = normalizeIdentityValue(ctx.turnId);
  const attempt = String(Math.max(1, ctx.attempt));

  return {
    headers: {
      ...sessionHeaders,
      "x-aether-turn-id": turnId,
      "x-aether-turn-attempt": attempt,
    },
    metadata: {
      aether_session_id: sessionHeaders["x-aether-session-id"] ?? "",
      aether_turn_id: turnId,
      aether_turn_attempt: attempt,
      aether_transport: ctx.transport,
    },
    ...(ctx.transport === "websocket"
      ? {
          websocket: {
            headers: sessionHeaders,
            degradeCooldownMs: DEFAULT_OPENAI_WS_DEGRADE_COOLDOWN_MS,
          },
        }
      : {}),
  };
}
