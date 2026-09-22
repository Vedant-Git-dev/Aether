// Legacy Talk config normalizer for provider shape and generic realtime aliases.
import { isDeepStrictEqual } from "node:util";
import { isRecord } from "@aether/normalization-core/record-coerce";
import { normalizeTalkSection } from "../../../config/talk.js";
import type { AetherConfig } from "../../../config/types.js";

function buildLegacyRealtimeTalkCompat(
  talk: Record<string, unknown>,
  normalizedTalk: NonNullable<AetherConfig["talk"]>,
): NonNullable<AetherConfig["talk"]>["realtime"] {
  if (talk.realtime !== undefined) {
    return undefined;
  }
  const compat: Record<string, unknown> = {};
  for (const key of ["model", "mode", "transport", "brain"] as const) {
    if (talk[key] !== undefined) {
      compat[key] = talk[key];
    }
  }
  if (talk.voice !== undefined) {
    compat.speakerVoice = talk.voice;
  }
  if (Object.keys(compat).length === 0) {
    return undefined;
  }
  if (normalizedTalk.provider !== undefined) {
    compat.provider = normalizedTalk.provider;
  }
  if (normalizedTalk.providers !== undefined) {
    compat.providers = normalizedTalk.providers;
  }
  return normalizeTalkSection({ realtime: compat } as AetherConfig["talk"])?.realtime;
}

/** Normalize Talk provider shape and move only core-owned legacy realtime fields. */
export function normalizeLegacyTalkConfig(cfg: AetherConfig, changes: string[]): AetherConfig {
  const rawTalk: unknown = cfg.talk;
  if (!isRecord(rawTalk)) {
    return cfg;
  }

  const normalizedTalk: Record<string, unknown> & NonNullable<AetherConfig["talk"]> =
    normalizeTalkSection(rawTalk as AetherConfig["talk"]) ?? {};
  for (const key of ["voiceId", "voiceAliases", "modelId", "outputFormat", "apiKey"] as const) {
    if (rawTalk[key] !== undefined) {
      normalizedTalk[key] = rawTalk[key];
    }
  }
  const legacyRealtimeCompat = buildLegacyRealtimeTalkCompat(rawTalk, normalizedTalk);
  if (legacyRealtimeCompat) {
    normalizedTalk.realtime = legacyRealtimeCompat;
  }
  if (Object.keys(normalizedTalk).length === 0 || isDeepStrictEqual(normalizedTalk, rawTalk)) {
    return cfg;
  }

  changes.push(
    "Normalized talk.provider/providers shape (trimmed provider ids and merged missing compatibility fields).",
  );
  if (legacyRealtimeCompat) {
    changes.push("Moved legacy realtime Talk provider/model fields into talk.realtime.");
  }
  return {
    ...cfg,
    talk: normalizedTalk,
  };
}
