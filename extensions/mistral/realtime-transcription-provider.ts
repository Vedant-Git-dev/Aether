import { createRealtimeTranscriptionWebSocketSession } from "aether/plugin-sdk/realtime-transcription-session";
import { buildMistralRealtimeTranscriptionProvider as createProvider } from "./realtime-transcription-provider-factory.js";

export function buildMistralRealtimeTranscriptionProvider() {
  return createProvider({ createRealtimeTranscriptionWebSocketSession });
}
