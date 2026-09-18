import type { AetherConfig } from "../config/types.aether.js";
import { mergeProcessEnv } from "./process-env.js";
import type { UpdateChannel } from "./update-channels.js";

export const POST_CORE_UPDATE_ENV = "AETHER_UPDATE_POST_CORE";
export const POST_CORE_UPDATE_CHANNEL_ENV = "AETHER_UPDATE_POST_CORE_CHANNEL";
export const POST_CORE_UPDATE_RESULT_PATH_ENV = "AETHER_UPDATE_POST_CORE_RESULT_PATH";
export const POST_CORE_UPDATE_INSTALL_RECORDS_PATH_ENV =
  "AETHER_UPDATE_POST_CORE_INSTALL_RECORDS_PATH";
export const POST_CORE_UPDATE_STARTED_AT_ENV = "AETHER_UPDATE_POST_CORE_STARTED_AT_MS";
export const POST_CORE_UPDATE_REQUESTED_CHANNEL_ENV = "AETHER_UPDATE_POST_CORE_REQUESTED_CHANNEL";
export const POST_CORE_UPDATE_SOURCE_CONFIG_PATH_ENV =
  "AETHER_UPDATE_POST_CORE_SOURCE_CONFIG_PATH";

export function buildPostCoreHandoffEnv(params: {
  baseEnv: NodeJS.ProcessEnv;
  compatHostVersion?: string | null;
  requestedChannel?: UpdateChannel | null;
  sourceConfigPath?: string;
}): NodeJS.ProcessEnv {
  return mergeProcessEnv([
    params.baseEnv,
    {
      AETHER_COMPATIBILITY_HOST_VERSION: params.compatHostVersion || undefined,
      [POST_CORE_UPDATE_REQUESTED_CHANNEL_ENV]: params.requestedChannel || undefined,
      [POST_CORE_UPDATE_SOURCE_CONFIG_PATH_ENV]: params.sourceConfigPath || undefined,
    },
  ]);
}

export type PreUpdateConfigRestoreInput = {
  sourceConfig: AetherConfig;
  authoredConfig: AetherConfig;
};
