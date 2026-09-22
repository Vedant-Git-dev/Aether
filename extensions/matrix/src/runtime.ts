import type { PluginRuntime } from "aether/plugin-sdk/plugin-runtime";
// Matrix plugin module implements runtime behavior.
import { createPluginRuntimeStore } from "aether/plugin-sdk/runtime-store";

const {
  setRuntime: setMatrixRuntime,
  getRuntime: getMatrixRuntime,
  tryGetRuntime: getOptionalMatrixRuntime,
} = createPluginRuntimeStore<PluginRuntime>({
  pluginId: "matrix",
  errorMessage: "Matrix runtime not initialized",
});

export { getMatrixRuntime, getOptionalMatrixRuntime, setMatrixRuntime };
