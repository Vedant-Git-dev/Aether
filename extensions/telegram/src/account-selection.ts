// Telegram plugin module implements account selection behavior.
import {
  createAccountListHelpers,
  hasConfiguredAccountValue,
  resolveListedDefaultAccountId,
} from "aether/plugin-sdk/account-core";
import {
  DEFAULT_ACCOUNT_ID,
  normalizeAccountId,
  normalizeOptionalAccountId,
} from "aether/plugin-sdk/account-id";
import { listAgentIds } from "aether/plugin-sdk/agent-scope-runtime";
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import { resolveDefaultAgentBoundAccountId } from "aether/plugin-sdk/routing";
import { normalizeLowercaseStringOrEmpty } from "aether/plugin-sdk/string-coerce-runtime";

function resolveBindingAccount(params: {
  binding: unknown;
  channelId: string;
}): { accountId: string } | null {
  if (!params.binding || typeof params.binding !== "object") {
    return null;
  }
  const binding = params.binding as {
    match?: { channel?: unknown; accountId?: unknown };
  };
  if (normalizeLowercaseStringOrEmpty(binding.match?.channel) !== params.channelId) {
    return null;
  }
  const accountId = typeof binding.match?.accountId === "string" ? binding.match.accountId : "";
  if (!accountId.trim() || accountId.trim() === "*") {
    return null;
  }
  return {
    accountId: normalizeAccountId(accountId),
  };
}

function listBoundAccountIds(cfg: AetherConfig, channelId: string): string[] {
  const ids = new Set<string>();
  for (const binding of cfg.bindings ?? []) {
    const resolved = resolveBindingAccount({ binding, channelId });
    if (resolved) {
      ids.add(resolved.accountId);
    }
  }
  return [...ids].toSorted((left, right) => left.localeCompare(right));
}

function hasImplicitDefaultTelegramAccount(cfg: AetherConfig): boolean {
  const telegram = cfg.channels?.telegram;
  if (!telegram) {
    return false;
  }
  return (
    hasConfiguredAccountValue(telegram.botToken) ||
    hasConfiguredAccountValue(telegram.tokenFile) ||
    hasConfiguredAccountValue(process.env.TELEGRAM_BOT_TOKEN)
  );
}

const { listAccountIds: listTelegramAccountIds } = createAccountListHelpers("telegram", {
  normalizeAccountId,
  additionalAccountIds: (cfg) => listBoundAccountIds(cfg, "telegram"),
  hasImplicitDefaultAccount: hasImplicitDefaultTelegramAccount,
});

export { listTelegramAccountIds };

export function resolveDefaultTelegramAccountSelection(cfg: AetherConfig): {
  accountId: string;
  accountIds: string[];
  shouldWarnMissingDefault: boolean;
} {
  // Explicit fleets use channel defaults, not a retained legacy migration owner.
  const boundDefault =
    cfg.agents?.ownership === "explicit" && listAgentIds(cfg).length !== 1
      ? null
      : resolveDefaultAgentBoundAccountId(cfg, "telegram");
  if (boundDefault) {
    return {
      accountId: boundDefault,
      accountIds: listTelegramAccountIds(cfg),
      shouldWarnMissingDefault: false,
    };
  }
  const accountIds = listTelegramAccountIds(cfg);
  const configuredDefaultAccountId =
    normalizeOptionalAccountId(cfg.channels?.telegram?.defaultAccount) ?? undefined;
  const hasExplicitDefaultAccount = configuredDefaultAccountId
    ? accountIds.includes(configuredDefaultAccountId)
    : false;
  const resolved = resolveListedDefaultAccountId({
    accountIds,
    configuredDefaultAccountId,
  });
  return {
    accountId: resolved,
    accountIds,
    shouldWarnMissingDefault:
      resolved === accountIds[0] &&
      !hasExplicitDefaultAccount &&
      !accountIds.includes(DEFAULT_ACCOUNT_ID) &&
      accountIds.length > 1,
  };
}

export function resolveDefaultTelegramAccountId(cfg: AetherConfig): string {
  return resolveDefaultTelegramAccountSelection(cfg).accountId;
}
