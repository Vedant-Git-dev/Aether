// Telegram account helpers resolve Telegram plugin account config and display metadata.
import type { AetherConfig } from "./config-contracts.js";
import { loadBundledPluginPublicSurfaceModuleSyncCore } from "./facade-loader.js";

/**
 * @deprecated Compatibility type for the `aether/plugin-sdk/telegram-account` facade.
 * New channel plugins should prefer injected runtime helpers and generic SDK subpaths.
 */
export type TelegramAccountConfig = NonNullable<
  NonNullable<AetherConfig["channels"]>["telegram"]
>;

/**
 * @deprecated Compatibility type for the `aether/plugin-sdk/telegram-account` facade.
 * New channel plugins should prefer injected runtime helpers and generic SDK subpaths.
 */
export type ResolvedTelegramAccount = {
  accountId: string;
  enabled: boolean;
  name?: string;
  token: string;
  tokenSource: "env" | "tokenFile" | "config" | "none";
  config: TelegramAccountConfig;
};

type TelegramAccountFacadeModule = {
  resolveTelegramAccount: (params: {
    cfg: AetherConfig;
    accountId?: string | null;
  }) => ResolvedTelegramAccount;
};

function loadTelegramAccountFacadeModule(): TelegramAccountFacadeModule {
  return loadBundledPluginPublicSurfaceModuleSyncCore<TelegramAccountFacadeModule>({
    dirName: "telegram",
    artifactBasename: "api.js",
  });
}

/**
 * @deprecated Compatibility facade for plugin code that needs Telegram account resolution.
 * New channel plugins should prefer injected runtime helpers and generic SDK subpaths.
 */
export function resolveTelegramAccount(params: {
  cfg: AetherConfig;
  accountId?: string | null;
}): ResolvedTelegramAccount {
  return loadTelegramAccountFacadeModule().resolveTelegramAccount(params);
}
