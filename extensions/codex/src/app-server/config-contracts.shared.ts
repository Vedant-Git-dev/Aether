export type AetherExecMode = "deny" | "allowlist" | "ask" | "auto" | "full";
export type AetherExecSecurity = "deny" | "allowlist" | "full";
export type AetherExecAsk = "off" | "on-miss" | "always";
export type AetherExecApprovalFloorsForCodexAppServer = {
  security?: AetherExecSecurity;
  ask?: AetherExecAsk;
};
export type AetherExecPolicyForCodexAppServer = {
  mode: AetherExecMode;
  security: AetherExecSecurity;
  ask: AetherExecAsk;
  touched: boolean;
};
export type AetherExecPolicy = AetherExecPolicyForCodexAppServer;

export type CodexAppServerCommandSource = "managed" | "resolved-managed" | "config" | "env";
export type CodexPluginDestructivePolicy = boolean | "auto" | "ask";
export type CodexPluginDestructiveApprovalMode = "allow" | "deny" | "auto" | "ask";

export const CODEX_PLUGIN_MARKETPLACE_NAME_PATTERN = /^[A-Za-z0-9_-]+$/;
export type CodexPluginMarketplaceName = string;

export type ResolvedCodexPluginPolicy = {
  configKey: string;
  marketplaceName: CodexPluginMarketplaceName;
  pluginName: string;
  enabled: boolean;
  allowDestructiveActions: boolean;
  destructiveApprovalMode: CodexPluginDestructiveApprovalMode;
};

export type ResolvedCodexPluginsPolicy = {
  configured: boolean;
  enabled: boolean;
  allowAllPlugins: boolean;
  allowDestructiveActions: boolean;
  destructiveApprovalMode: CodexPluginDestructiveApprovalMode;
  pluginPolicies: ResolvedCodexPluginPolicy[];
};
