// Imessage plugin module implements runtime behavior.
import fs from "node:fs";
import path from "node:path";
import type {
  OpenKeyedStoreOptions,
  PluginStateSyncKeyedStore,
} from "aether/plugin-sdk/plugin-state-runtime";
import {
  closeAetherStateDatabaseForTest,
  createChannelIngressQueueForTests,
  createPluginStateKeyedStoreForTests,
  createPluginStateSyncKeyedStoreForTests,
  resetPluginStateStoreForTests,
} from "aether/plugin-sdk/plugin-state-test-runtime";
import type { PluginRuntime } from "aether/plugin-sdk/runtime-store";
import { resolvePreferredAetherTmpDir } from "aether/plugin-sdk/temp-path";
import { vi } from "vitest";
import { setIMessageRuntime } from "../runtime.js";

function createIMessageTestEnv(): NodeJS.ProcessEnv & { AETHER_STATE_DIR: string } {
  const stateDir = fs.realpathSync(
    fs.mkdtempSync(path.join(resolvePreferredAetherTmpDir(), "aether-imessage-state-")),
  );
  return { ...process.env, AETHER_STATE_DIR: stateDir };
}

let imessageTestEnv = createIMessageTestEnv();

export function createIMessagePluginStateSyncStoreForTest<T>(
  options: OpenKeyedStoreOptions,
): PluginStateSyncKeyedStore<T> {
  return createPluginStateSyncKeyedStoreForTests<T>("imessage", {
    ...options,
    env: imessageTestEnv,
  });
}

export function installIMessageStateRuntimeForTest(): void {
  closeAetherStateDatabaseForTest();
  imessageTestEnv = createIMessageTestEnv();
  resetPluginStateStoreForTests();
  setIMessageRuntime({
    state: {
      resolveStateDir: () => imessageTestEnv.AETHER_STATE_DIR,
      openChannelIngressQueue: (
        options?: Omit<Parameters<typeof createChannelIngressQueueForTests>[0], "channelId">,
      ) =>
        createChannelIngressQueueForTests({
          ...options,
          channelId: "imessage",
          stateDir: options?.stateDir ?? imessageTestEnv.AETHER_STATE_DIR,
        }),
      openKeyedStore: ((options) =>
        createPluginStateKeyedStoreForTests("imessage", {
          ...options,
          env: imessageTestEnv,
        })) as PluginRuntime["state"]["openKeyedStore"],
      openSyncKeyedStore: ((options) =>
        createIMessagePluginStateSyncStoreForTest(
          options,
        )) as PluginRuntime["state"]["openSyncKeyedStore"],
    },
    channel: {},
  } as PluginRuntime);
  createIMessagePluginStateSyncStoreForTest({
    namespace: "imessage.reply-cache",
    maxEntries: 2000,
  }).entries();
  createIMessagePluginStateSyncStoreForTest({
    namespace: "imessage.reply-cache-counter",
    maxEntries: 1,
  }).entries();
}

export async function loadFreshIMessageReplyCacheForTest(options?: {
  preservePersistentState?: boolean;
}): Promise<typeof import("../monitor-reply-cache.js")> {
  if (!options?.preservePersistentState) {
    closeAetherStateDatabaseForTest();
    imessageTestEnv = createIMessageTestEnv();
  }
  resetPluginStateStoreForTests();
  vi.resetModules();
  const { setIMessageRuntime: setFreshIMessageRuntime } = await import("../runtime.js");
  setFreshIMessageRuntime({
    state: {
      resolveStateDir: () => imessageTestEnv.AETHER_STATE_DIR,
      openChannelIngressQueue: (
        queueOptions?: Omit<Parameters<typeof createChannelIngressQueueForTests>[0], "channelId">,
      ) =>
        createChannelIngressQueueForTests({
          ...queueOptions,
          channelId: "imessage",
          stateDir: queueOptions?.stateDir ?? imessageTestEnv.AETHER_STATE_DIR,
        }),
      openKeyedStore: ((storeOptions) =>
        createPluginStateKeyedStoreForTests("imessage", {
          ...storeOptions,
          env: imessageTestEnv,
        })) as PluginRuntime["state"]["openKeyedStore"],
      openSyncKeyedStore: ((storeOptions) =>
        createIMessagePluginStateSyncStoreForTest(
          storeOptions,
        )) as PluginRuntime["state"]["openSyncKeyedStore"],
    },
    channel: {},
  } as PluginRuntime);
  createIMessagePluginStateSyncStoreForTest({
    namespace: "imessage.reply-cache",
    maxEntries: 2000,
  }).entries();
  createIMessagePluginStateSyncStoreForTest({
    namespace: "imessage.reply-cache-counter",
    maxEntries: 1,
  }).entries();
  return await import("../monitor-reply-cache.js");
}

export function installIMessageFailingStateRuntimeForTest(): void {
  closeAetherStateDatabaseForTest();
  imessageTestEnv = createIMessageTestEnv();
  setIMessageRuntime({
    state: {
      resolveStateDir: () => imessageTestEnv.AETHER_STATE_DIR,
      openChannelIngressQueue: (
        options?: Omit<Parameters<typeof createChannelIngressQueueForTests>[0], "channelId">,
      ) =>
        createChannelIngressQueueForTests({
          ...options,
          channelId: "imessage",
          stateDir: options?.stateDir ?? imessageTestEnv.AETHER_STATE_DIR,
        }),
      openKeyedStore: (() => {
        throw new Error("test plugin-state failure");
      }) as PluginRuntime["state"]["openKeyedStore"],
      openSyncKeyedStore: (() => {
        throw new Error("test plugin-state failure");
      }) as PluginRuntime["state"]["openSyncKeyedStore"],
    },
    channel: {},
  } as PluginRuntime);
}
