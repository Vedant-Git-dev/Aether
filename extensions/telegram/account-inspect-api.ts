// Telegram API module exposes the plugin public contract.
import type { AetherConfig } from "./runtime-api.js";
import { inspectTelegramAccount } from "./src/account-inspect.js";

export function inspectTelegramReadOnlyAccount(cfg: AetherConfig, accountId?: string | null) {
  return inspectTelegramAccount({ cfg, accountId });
}
