import type { AetherConfig } from "../../config/types.aether.js";
import type { ChannelConfigSchema } from "./types.config.js";

type ManifestChannelAccount = {
  accountId: string;
  name?: string;
  config: Record<string, unknown>;
};

/** Metadata adapters expose account inspection without loading channel runtime contracts. */
export type ManifestChannelPlugin = {
  id: string;
  meta: {
    id: string;
    label: string;
    selectionLabel: string;
    detailLabel?: string;
    systemImage?: string;
    docsPath: string;
    blurb: string;
    preferOver?: readonly string[];
  };
  capabilities: { chatTypes: ["direct"] };
  commands?: {
    nativeCommandsAutoEnabled?: boolean;
    nativeSkillsAutoEnabled?: boolean;
  };
  configSchema?: ChannelConfigSchema;
  config: {
    listAccountIds: (cfg: AetherConfig) => string[];
    defaultAccountId: (cfg: AetherConfig) => string;
    resolveAccount: (cfg: AetherConfig, accountId?: string | null) => ManifestChannelAccount;
    isEnabled: (account: ManifestChannelAccount, cfg: AetherConfig) => boolean;
    isConfigured: (account: ManifestChannelAccount, cfg: AetherConfig) => boolean;
    hasConfiguredState: (params: { cfg: AetherConfig; env?: NodeJS.ProcessEnv }) => boolean;
  };
};
