/**
 * Browser-local SDK setup/tooling bridge for CLI, media, and action helpers.
 */
export {
  callGatewayTool,
  hasGatewayToolRoutingContext,
  listNodes,
  resolveNodeIdFromList,
} from "aether/plugin-sdk/agent-harness-runtime";
export type { AnyAgentTool } from "aether/plugin-sdk/agent-harness-runtime";
export {
  imageResultFromFile,
  jsonResult,
  readPositiveIntegerParam,
  readStringParam,
} from "aether/plugin-sdk/channel-actions";
export { formatCliCommand, note } from "aether/plugin-sdk/cli-runtime";
export {
  IMAGE_REDUCE_QUALITY_STEPS,
  buildImageResizeSideGrid,
  getImageMetadata,
  isImageProcessorUnavailableError,
  resizeToJpeg,
} from "aether/plugin-sdk/media-runtime";
export { detectMime } from "aether/plugin-sdk/media-mime";
export { ensureMediaDir, saveMediaBuffer } from "aether/plugin-sdk/media-runtime";
export { describeImageFile } from "aether/plugin-sdk/media-understanding-runtime";
