// Discord API module exposes the plugin public contract.
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import { inspectDiscordAccount } from "./src/account-inspect.js";

export function inspectDiscordReadOnlyAccount(cfg: AetherConfig, accountId?: string | null) {
  return inspectDiscordAccount({ cfg, accountId });
}
