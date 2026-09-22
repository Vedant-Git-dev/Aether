import { MeetingPlatformAdapter } from "aether/plugin-sdk/meeting-runtime";
import { addTimerTimeoutGraceMs } from "aether/plugin-sdk/number-runtime";
import { REALTIME_VOICE_AGENT_CONSULT_TOOL_NAME } from "aether/plugin-sdk/realtime-voice";

export const teamsMeetingsConfig = MeetingPlatformAdapter.createPluginConfigSchema({
  defaultRealtimeInstructions: `You are joining a private Microsoft Teams meeting as an Aether voice transport. Keep spoken replies brief and natural. In agent mode, wait for Aether consult results and speak them exactly. In bidi mode, answer directly and call ${REALTIME_VOICE_AGENT_CONSULT_TOOL_NAME} for deeper reasoning, current information, or tools.`,
  resolveGatewayOperationTimeoutMs: (config) =>
    Math.max(60_000, addTimerTimeoutGraceMs(config.chrome.joinTimeoutMs, 30_000) ?? 1),
});

export type TeamsMeetingsConfig = ReturnType<typeof teamsMeetingsConfig.resolveConfig>;
export type TeamsMeetingsMode = TeamsMeetingsConfig["defaultMode"];
export type TeamsMeetingsTransport = "chrome" | "chrome-node";
