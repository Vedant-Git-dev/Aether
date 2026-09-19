// Searxng helper module supports config behavior.
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import { normalizeSecretInput } from "aether/plugin-sdk/secret-input";
import { resolveReadOnlyEnvSecretRef } from "aether/plugin-sdk/secret-ref-readonly";
import { normalizeOptionalString } from "aether/plugin-sdk/string-coerce-runtime";

const SEARXNG_BASE_URL_ENV_VAR = "SEARXNG_BASE_URL";
const SEARXNG_BASE_URL_PATH = "plugins.entries.searxng.config.webSearch.baseUrl";

type SearxngPluginConfig = {
  webSearch?: {
    baseUrl?: unknown;
    categories?: string;
    language?: string;
  };
};

function normalizeBaseUrl(value: unknown): string | undefined {
  return normalizeSecretInput(value)?.replace(/\/+$/u, "") || undefined;
}

function resolveSearxngWebSearchConfig(
  config?: AetherConfig,
): SearxngPluginConfig["webSearch"] | undefined {
  const pluginConfig = config?.plugins?.entries?.searxng?.config as SearxngPluginConfig | undefined;
  const webSearch = pluginConfig?.webSearch;
  if (webSearch && typeof webSearch === "object" && !Array.isArray(webSearch)) {
    return webSearch;
  }
  return undefined;
}

export function resolveSearxngBaseUrl(config?: AetherConfig): string | undefined {
  const webSearch = resolveSearxngWebSearchConfig(config);
  const resolved = resolveReadOnlyEnvSecretRef({
    value: webSearch?.baseUrl,
    path: SEARXNG_BASE_URL_PATH,
    cfg: config,
    expectedEnvId: SEARXNG_BASE_URL_ENV_VAR,
    normalizeValue: normalizeBaseUrl,
  });
  if (resolved.status === "available") {
    return resolved.value;
  }
  if (resolved.status === "blocked") {
    return undefined;
  }
  return normalizeBaseUrl(process.env[SEARXNG_BASE_URL_ENV_VAR]);
}

export function resolveSearxngCategories(config?: AetherConfig): string | undefined {
  return normalizeOptionalString(resolveSearxngWebSearchConfig(config)?.categories);
}

export function resolveSearxngLanguage(config?: AetherConfig): string | undefined {
  return normalizeOptionalString(resolveSearxngWebSearchConfig(config)?.language);
}
