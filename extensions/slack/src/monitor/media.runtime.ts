// Slack plugin module implements media behavior.
import { createSubsystemLogger } from "aether/plugin-sdk/runtime-env";

export const slackMediaLog = createSubsystemLogger("gateway/channels/slack").child("media");
export { fetchWithRuntimeDispatcher } from "aether/plugin-sdk/runtime-fetch";
export type { FetchLike } from "aether/plugin-sdk/media-runtime";
export { saveRemoteMedia } from "aether/plugin-sdk/media-runtime";
