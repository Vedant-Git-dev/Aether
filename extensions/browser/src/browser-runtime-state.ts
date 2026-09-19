import type { PluginStateSyncKeyedStore } from "aether/plugin-sdk/plugin-state-runtime";
// Browser plugin runtime state shared across lazy bundles and duplicate SDK module instances.
import { createPluginRuntimeStore } from "aether/plugin-sdk/runtime-store";

type BrowserStateRuntime = {
  sessionTabs: PluginStateSyncKeyedStore<unknown>;
};

const {
  setRuntime: setBrowserStateRuntime,
  getRuntime: getBrowserStateRuntime,
  tryGetRuntime: getOptionalBrowserStateRuntime,
} = createPluginRuntimeStore<BrowserStateRuntime>({
  pluginId: "browser",
  errorMessage: "Browser state runtime not initialized",
});

export { getBrowserStateRuntime, getOptionalBrowserStateRuntime, setBrowserStateRuntime };
