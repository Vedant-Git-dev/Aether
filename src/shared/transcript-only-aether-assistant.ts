// Identifies Aether-authored assistant rows that are transcript bookkeeping,
// not provider model output. Some history surfaces keep gateway-injected rows
// visible, so use the narrower delivery-mirror predicate when visibility matters.
export const AETHER_TRANSCRIPT_ARTIFACT_API = "aether-transcript" as const;
export const AETHER_TRANSCRIPT_ARTIFACT_PROVIDER = "aether" as const;
export const AETHER_DELIVERY_MIRROR_MODEL = "delivery-mirror" as const;
export const CRON_DIRECT_DELIVERY_CONTEXT_KIND = "cron-direct-delivery-context" as const;
const AETHER_GATEWAY_INJECTED_MODEL = "gateway-injected" as const;

const TRANSCRIPT_ONLY_AETHER_ASSISTANT_MODELS = new Set<string>([
  AETHER_DELIVERY_MIRROR_MODEL,
  AETHER_GATEWAY_INJECTED_MODEL,
]);
const AETHER_DELIVERY_MIRROR_KINDS = new Set([
  "channel-final",
  "channel-final-suppressed",
  "message-tool-source-reply",
  CRON_DIRECT_DELIVERY_CONTEXT_KIND,
]);

function isAetherDeliveryMirrorMarker(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const kind = (value as { kind?: unknown }).kind;
  return typeof kind === "string" && AETHER_DELIVERY_MIRROR_KINDS.has(kind);
}

export function isTranscriptOnlyAetherAssistantModel(provider: unknown, model: unknown): boolean {
  return (
    provider === AETHER_TRANSCRIPT_ARTIFACT_PROVIDER &&
    typeof model === "string" &&
    TRANSCRIPT_ONLY_AETHER_ASSISTANT_MODELS.has(model)
  );
}

/**
 * Returns true when the message is an Aether-authored transcript artifact
 * that must not be replayed to providers.
 *
 * Primary check: provider="aether" + model in known transcript-only set.
 * Fallback: a valid aetherDeliveryMirror marker catches observed historical
 * rows whose provider/model provenance was stripped (#99470).
 */
export function isTranscriptOnlyAetherAssistantMessage(message: unknown): boolean {
  if (!message || typeof message !== "object" || Array.isArray(message)) {
    return false;
  }
  const entry = message as {
    role?: unknown;
    provider?: unknown;
    model?: unknown;
    aetherDeliveryMirror?: unknown;
  };
  if (entry.role !== "assistant") {
    return false;
  }
  if (isTranscriptOnlyAetherAssistantModel(entry.provider, entry.model)) {
    return true;
  }
  return isAetherDeliveryMirrorMarker(entry.aetherDeliveryMirror);
}

export function isAetherMessageToolMirrorAssistantMessage(message: unknown): boolean {
  if (!message || typeof message !== "object" || Array.isArray(message)) {
    return false;
  }
  const entry = message as { role?: unknown; aetherMessageToolMirror?: unknown };
  return entry.role === "assistant" && entry.aetherMessageToolMirror !== undefined;
}

export function isAetherDeliveryMirrorAssistantMessage(message: unknown): boolean {
  if (!message || typeof message !== "object" || Array.isArray(message)) {
    return false;
  }
  const entry = message as { role?: unknown; provider?: unknown; model?: unknown };
  return (
    entry.role === "assistant" &&
    entry.provider === AETHER_TRANSCRIPT_ARTIFACT_PROVIDER &&
    entry.model === AETHER_DELIVERY_MIRROR_MODEL
  );
}
