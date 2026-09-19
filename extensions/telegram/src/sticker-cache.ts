// Telegram plugin module implements sticker cache behavior.
import { resolveAgentDir, resolveDefaultModelForAgent } from "aether/plugin-sdk/agent-runtime";
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import { resolveAutoImageModel } from "aether/plugin-sdk/media-runtime";
import { logVerbose } from "aether/plugin-sdk/runtime-env";
import { getTelegramRuntime } from "./runtime.js";
export {
  cacheSticker,
  getAllCachedStickers,
  getCachedSticker,
  getCacheStats,
  searchStickers,
  type CachedSticker,
} from "./sticker-cache-store.js";

const STICKER_DESCRIPTION_PROMPT =
  "Describe this sticker image in 1-2 sentences. Focus on what the sticker depicts (character, object, action, emotion). Be concise and objective.";

export interface DescribeStickerParams {
  imagePath: string;
  cfg: AetherConfig;
  agentDir?: string;
  agentId?: string;
}

/**
 * Describe a sticker image using vision API.
 * Uses the shared image-model policy, then describes the sticker once.
 * Returns null if no model is selected or description fails.
 */
export async function describeStickerImage(params: DescribeStickerParams): Promise<string | null> {
  const { imagePath, cfg, agentDir, agentId } = params;

  const scopedAgentDir = agentDir ?? (agentId ? resolveAgentDir(cfg, agentId) : undefined);
  const activeModel = resolveDefaultModelForAgent({ cfg, agentId });
  const resolved = await resolveAutoImageModel({
    cfg,
    agentId,
    agentDir: scopedAgentDir,
    activeModel,
  });

  if (!resolved?.model) {
    logVerbose("telegram: no vision provider available for sticker description");
    return null;
  }

  const { provider, model } = resolved;
  logVerbose(`telegram: describing sticker with ${provider}/${model}`);

  try {
    const result = await getTelegramRuntime().mediaUnderstanding.describeImageFileWithModel({
      filePath: imagePath,
      mime: "image/webp",
      cfg,
      agentDir: scopedAgentDir,
      provider,
      model,
      prompt: STICKER_DESCRIPTION_PROMPT,
      maxTokens: 150,
      timeoutMs: 30_000,
    });
    return result.text ?? null;
  } catch (err) {
    logVerbose(`telegram: failed to describe sticker: ${String(err)}`);
    return null;
  }
}
