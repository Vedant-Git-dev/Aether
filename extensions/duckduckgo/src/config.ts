// Duckduckgo helper module supports config behavior.
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import {
  normalizeLowercaseStringOrEmpty,
  normalizeOptionalString,
} from "aether/plugin-sdk/string-coerce-runtime";

const DEFAULT_DDG_SAFE_SEARCH = "moderate";

export type DdgSafeSearch = "strict" | "moderate" | "off";

type DdgPluginConfig = {
  webSearch?: {
    region?: string;
    safeSearch?: string;
  };
};

function resolveDdgWebSearchConfig(
  config?: AetherConfig,
): DdgPluginConfig["webSearch"] | undefined {
  const pluginConfig = config?.plugins?.entries?.duckduckgo?.config as DdgPluginConfig | undefined;
  const webSearch = pluginConfig?.webSearch;
  if (webSearch && typeof webSearch === "object" && !Array.isArray(webSearch)) {
    return webSearch;
  }
  return undefined;
}

export function resolveDdgRegion(config?: AetherConfig): string | undefined {
  return normalizeOptionalString(resolveDdgWebSearchConfig(config)?.region);
}

export function resolveDdgSafeSearch(config?: AetherConfig): DdgSafeSearch {
  const safeSearch = resolveDdgWebSearchConfig(config)?.safeSearch;
  const normalized = normalizeLowercaseStringOrEmpty(safeSearch);
  if (normalized === "strict" || normalized === "off") {
    return normalized;
  }
  return DEFAULT_DDG_SAFE_SEARCH;
}
