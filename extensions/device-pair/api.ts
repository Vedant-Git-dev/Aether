// Device Pair API module exposes the plugin public contract.
export {
  approveDevicePairing,
  clearDeviceBootstrapTokens,
  issueDeviceBootstrapToken,
  PAIRING_SETUP_BOOTSTRAP_PROFILE,
  listDevicePairing,
  revokeDeviceBootstrapToken,
  type DeviceBootstrapProfile,
} from "aether/plugin-sdk/device-bootstrap";
export { definePluginEntry, type AetherPluginApi } from "aether/plugin-sdk/plugin-entry";
export {
  resolveGatewayBindUrl,
  resolveTailnetHostWithRunner,
  resolveTailscaleServeGatewayUrlsWithRunner,
} from "aether/plugin-sdk/core";
export { resolveAdvertisedLanHost } from "aether/plugin-sdk/gateway-runtime";
export {
  resolvePreferredAetherTmpDir,
  runPluginCommandWithTimeout,
} from "aether/plugin-sdk/sandbox";
export { resolveGatewayPort } from "aether/plugin-sdk/gateway-config-runtime";
export { renderQrPngBase64, renderQrPngDataUrl, writeQrPngTempFile } from "./qr-image.js";
