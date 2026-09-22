import {
  findConfiguredProviderModel,
  projectModelProviderConfig,
  resolveMergedModelProviderConfig,
} from "../config/model-provider-config.js";
import type { AetherConfig } from "../config/types.aether.js";
import { createProviderModelCatalogIdNormalizer } from "../plugins/provider-model-routes.js";
import {
  resolveProviderIdForAuth,
  type ProviderAuthAliasLookupParams,
} from "./provider-auth-aliases.js";

type ModelProviderAuthConfigParams = ProviderAuthAliasLookupParams & {
  provider: string;
  modelId?: string;
  modelBaseUrl?: unknown;
};

export function resolveModelProviderAuthConfig(
  params: ModelProviderAuthConfigParams & { config: AetherConfig },
): AetherConfig;
export function resolveModelProviderAuthConfig(
  params: ModelProviderAuthConfigParams,
): AetherConfig | undefined;
/** Endpoint-conditioned aliases follow the selected model before any auth state mutation. */
export function resolveModelProviderAuthConfig(
  params: ModelProviderAuthConfigParams,
): AetherConfig | undefined {
  const modelBaseUrl =
    params.modelBaseUrl ??
    (params.modelId
      ? findConfiguredProviderModel(
          resolveMergedModelProviderConfig(params.config, params.provider),
          params.provider,
          params.modelId,
          createProviderModelCatalogIdNormalizer(params.provider, params.metadataSnapshot),
        )?.baseUrl?.trim()
      : undefined);
  if (typeof modelBaseUrl !== "string" || !modelBaseUrl) {
    return params.config;
  }
  const config = projectModelProviderConfig(params.config, params.provider, {
    baseUrl: modelBaseUrl,
  });
  return resolveProviderIdForAuth(params.provider, params) ===
    resolveProviderIdForAuth(params.provider, { ...params, config })
    ? params.config
    : config;
}
