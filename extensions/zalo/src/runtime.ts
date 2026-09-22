import type { PluginRuntime } from "aether/plugin-sdk/core";
// Zalo plugin module implements runtime behavior.
import { createPluginRuntimeStore } from "aether/plugin-sdk/runtime-store";

const { setRuntime: setZaloRuntime, getRuntime: getZaloRuntime } =
  createPluginRuntimeStore<PluginRuntime>({
    pluginId: "zalo",
    errorMessage: "Zalo runtime not initialized",
  });
export { getZaloRuntime, setZaloRuntime };
