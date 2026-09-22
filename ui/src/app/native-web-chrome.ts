// Stub: native web chrome integration (mobile-only, stubbed for desktop builds)
export type NativeHistoryState = Record<string, unknown>;
export const NATIVE_HISTORY_STATE_EVENT = "native-history-state";
export function readNativeHistoryState(): NativeHistoryState | null { return null; }
export function isNativeEmbedHost(): boolean { return false; }
export function isNativeWebChromeHost(): boolean { return false; }
