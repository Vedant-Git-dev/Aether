import type { PluginCapabilityCatalogEntry } from "aether/plugin-sdk/plugin-entry";
import { buildDeepgramRealtimeTranscriptionProvider } from "./realtime-transcription-provider-factory.js";

export default ((context) => ({
  realtimeTranscriptionProviders: [buildDeepgramRealtimeTranscriptionProvider(context)],
})) satisfies PluginCapabilityCatalogEntry;
