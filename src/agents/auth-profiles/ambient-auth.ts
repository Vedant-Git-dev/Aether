/** Provider auth-pin policy for credentials discovered outside Aether storage. */
import { findNormalizedProviderValue } from "@aether/model-catalog-core/provider-id";
import type { AetherConfig } from "../../config/types.aether.js";
import {
  type ProviderAuthAliasLookupParams,
  resolveProviderIdForAuth,
} from "../provider-auth-aliases.js";
import type { AuthProfileCredential } from "./types.js";

/** Returns whether ambient credential material agrees with a provider's declared auth mode. */
export function isAmbientCredentialAllowedByProviderAuthPin(params: {
  config?: AetherConfig;
  authAliasLookupParams?: Omit<ProviderAuthAliasLookupParams, "config">;
  provider: string;
  type: AuthProfileCredential["type"];
}): boolean {
  const providers = params.config?.models?.providers;
  const direct = findNormalizedProviderValue(providers, params.provider);
  const providerAuthKey = resolveProviderIdForAuth(params.provider, {
    config: params.config,
    ...params.authAliasLookupParams,
  });
  const auth = direct?.auth ?? findNormalizedProviderValue(providers, providerAuthKey)?.auth;
  if (auth === "api-key") {
    return params.type === "api_key";
  }
  if (auth === "oauth") {
    return params.type === "oauth" || params.type === "token";
  }
  if (auth === "token") {
    return params.type === "token";
  }
  return auth === undefined;
}
