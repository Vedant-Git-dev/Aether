// Whatsapp plugin module implements doctor contract behavior.
import type {
  ChannelDoctorConfigMutation,
  ChannelDoctorLegacyConfigRule,
} from "aether/plugin-sdk/channel-contract";
import type { AetherConfig } from "aether/plugin-sdk/config-contracts";
import {
  asObjectRecord,
  defineChannelAliasMigration,
  hasLegacyAccountStreamingAliases,
  stripRetiredChannelKeys,
} from "aether/plugin-sdk/runtime-doctor-migrations";
import { normalizeCompatibilityConfig as normalizeAckReactionConfig } from "./doctor.js";

// WhatsApp's nested streaming schema is delivery-only ({chunkMode, block});
// it has no preview mode, so only the delivery flat aliases are legal legacy
// input. WhatsApp resolution layers accounts.default shared config between the
// channel root and named accounts, so the shared migration materializes that
// inheritance when it creates a named-account streaming object.
const streamingAliasMigration = defineChannelAliasMigration({
  channelId: "whatsapp",
  streaming: { defaultMode: "partial", deliveryOnly: true },
  accountStreamingInheritsDefaultAccount: true,
});

const hasExposeErrorText = (value: unknown): boolean =>
  Object.hasOwn(asObjectRecord(value) ?? {}, "exposeErrorText");

const hasAckReaction = (value: unknown): boolean =>
  Boolean(asObjectRecord(asObjectRecord(value)?.ackReaction));

export const legacyConfigRules: ChannelDoctorLegacyConfigRule[] = [
  ...streamingAliasMigration.legacyConfigRules,
  {
    path: ["channels", "whatsapp", "ackReaction"],
    message:
      'channels.whatsapp.ackReaction moved to global message acknowledgement settings. Run "aether doctor --fix".',
  },
  {
    path: ["channels", "whatsapp", "accounts"],
    message:
      'channels.whatsapp.accounts.<id>.ackReaction moved to global message acknowledgement settings. Run "aether doctor --fix".',
    match: (value) => hasLegacyAccountStreamingAliases(value, hasAckReaction),
  },
  {
    path: ["channels", "whatsapp", "exposeErrorText"],
    message:
      'channels.whatsapp.exposeErrorText is retired and ignored. Run "aether doctor --fix".',
  },
  {
    path: ["channels", "whatsapp", "accounts"],
    message:
      'channels.whatsapp.accounts.<id>.exposeErrorText is retired and ignored. Run "aether doctor --fix".',
    match: (value) => hasLegacyAccountStreamingAliases(value, hasExposeErrorText),
  },
];

function removeExposeErrorText(cfg: AetherConfig, changes: string[]): AetherConfig {
  return stripRetiredChannelKeys({
    cfg,
    channelId: "whatsapp",
    keys: new Set(["exposeErrorText"]),
    scope: "root-and-accounts",
    onRemove: ({ key, pathPrefix }) => changes.push(`Removed retired ${pathPrefix}.${key}.`),
  }).config;
}

export function normalizeCompatibilityConfig({
  cfg,
}: {
  cfg: AetherConfig;
}): ChannelDoctorConfigMutation {
  const ackReaction = normalizeAckReactionConfig({ cfg });
  const retiredConfig = removeExposeErrorText(ackReaction.config, ackReaction.changes);
  return streamingAliasMigration.normalizeChannelConfig({
    cfg: retiredConfig,
    changes: ackReaction.changes,
  });
}
