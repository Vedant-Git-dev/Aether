import type { StreamFn } from "aether/plugin-sdk/agent-core";
import { createLazyRuntimeModule } from "aether/plugin-sdk/lazy-runtime";
import type { AetherPluginApi } from "aether/plugin-sdk/plugin-entry";

const loadOllamaStreamRuntime = createLazyRuntimeModule(() => import("./stream.runtime.js"));

export type OllamaLocalService = {
  providerId: string;
  acquire: AetherPluginApi["runtime"]["llm"]["acquireLocalService"];
};

export function createLazyConfiguredOllamaStreamFn(params: {
  model: { baseUrl?: string; headers?: unknown };
  localService?: OllamaLocalService;
  providerBaseUrl?: string;
}): StreamFn {
  const streamFnPromise = loadOllamaStreamRuntime().then((runtime) =>
    runtime.createConfiguredOllamaStreamFn(params),
  );
  return async (...args) => {
    const streamFn = await streamFnPromise;
    return streamFn(...args);
  };
}
