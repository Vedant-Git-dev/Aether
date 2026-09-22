import type { AetherConfig } from "aether/plugin-sdk/config-contracts";

export type A2aPeerConfig = {
  token: string;
  url?: string;
  outboundToken?: string;
};

export type A2aChannelConfig = {
  enabled?: boolean;
  configWrites?: boolean;
  advertisedUrl?: string;
  replyTimeoutMs?: number;
  rateLimitPerMinute?: number;
  exposeAgents?: string[];
  peers?: Record<string, A2aPeerConfig>;
};

export type A2aCoreConfig = AetherConfig & {
  channels?: AetherConfig["channels"] & {
    a2a?: A2aChannelConfig;
  };
};

export type ResolvedA2aChannelAccount = {
  accountId: string;
  enabled: boolean;
  configured: boolean;
  config: A2aChannelConfig;
};
