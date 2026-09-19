// Slack API module exposes the plugin public contract.
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import { inspectSlackAccount } from "./src/account-inspect.js";

export function inspectSlackReadOnlyAccount(cfg: AetherConfig, accountId?: string | null) {
  return inspectSlackAccount({ cfg, accountId });
}
