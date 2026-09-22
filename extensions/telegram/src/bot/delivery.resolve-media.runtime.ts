// Telegram plugin module implements delivery.resolve media behavior.
import { logVerbose, sleepWithAbort } from "aether/plugin-sdk/runtime-env";
import { formatErrorMessage } from "aether/plugin-sdk/ssrf-runtime";
import { resolveTelegramApiBase, shouldRetryTelegramTransportFallback } from "../fetch.js";
import { MediaFetchError, saveMediaBuffer, saveRemoteMedia } from "../telegram-media.runtime.js";

export {
  formatErrorMessage,
  logVerbose,
  MediaFetchError,
  resolveTelegramApiBase,
  sleepWithAbort,
  saveMediaBuffer,
  saveRemoteMedia,
  shouldRetryTelegramTransportFallback,
};
