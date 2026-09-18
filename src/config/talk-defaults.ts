/** Platform-specific silence windows for talk/voice turn segmentation. */
const TALK_SILENCE_TIMEOUT_MS_BY_PLATFORM = {
  macos: 700,
} as const;

/** Formats the talk silence defaults for config help text. */
export function describeTalkSilenceTimeoutDefaults(): string {
  return `${TALK_SILENCE_TIMEOUT_MS_BY_PLATFORM.macos} ms`;
}
