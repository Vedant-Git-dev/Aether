import fs from "node:fs/promises";
import path from "node:path";
import { expect, vi } from "vitest";
import { listAgentEntries } from "../agents/agent-scope-config.js";
import { createConfigFileSnapshot } from "../config/io.snapshot-shared.js";
import type { ConfigFileSnapshot, AetherConfig } from "../config/types.aether.js";
// Non-interactive onboarding test helpers build runtime stubs that throw instead of exiting.
import type { RuntimeEnv } from "../runtime.js";
import { captureEnv, deleteTestEnvValue, setTestEnvValue } from "../test-utils/env.js";

type RuntimeLike = Pick<RuntimeEnv, "log" | "error" | "exit">;

type NonInteractiveRuntime = {
  log: RuntimeLike["log"];
  error: RuntimeLike["error"];
  exit: RuntimeLike["exit"];
};

export type WaitForGatewayReachableMock =
  | ((params: {
      url: string;
      token?: string;
      password?: string;
      deadlineMs?: number;
      probeTimeoutMs?: number;
    }) => Promise<{ ok: boolean; detail?: string }>)
  | undefined;

type RunNonInteractiveSetup = typeof import("./onboard-non-interactive.js").runNonInteractiveSetup;

type MockWithCalls<TArgs extends unknown[]> = {
  mock: {
    calls: TArgs[];
  };
};

export type OnboardEnsureWorkspaceOptions = {
  skipBootstrap?: boolean;
};

export type OnboardGatewayHealthCall = {
  password?: string;
  token?: string;
};

export type OnboardHealthCommandCall = OnboardGatewayHealthCall & {
  config?: AetherConfig;
};

export function createThrowingRuntime(): NonInteractiveRuntime {
  return {
    log: () => {},
    error: (...args: unknown[]) => {
      throw new Error(args.map(String).join(" "));
    },
    exit: (code: number) => {
      throw new Error(`exit:${code}`);
    },
  };
}

export function createOnboardJsonCaptureRuntime() {
  let capturedJson = "";
  const runtimeWithCapture: RuntimeEnv = {
    log: (...args: unknown[]) => {
      const firstArg = args[0];
      capturedJson =
        typeof firstArg === "string"
          ? firstArg
          : firstArg instanceof Error
            ? firstArg.message
            : (JSON.stringify(firstArg) ?? "");
    },
    error: (...args: unknown[]) => {
      const firstArg = args[0];
      const capturedError =
        typeof firstArg === "string"
          ? firstArg
          : firstArg instanceof Error
            ? firstArg.message
            : (JSON.stringify(firstArg) ?? "");
      throw new Error(capturedError);
    },
    exit: (_code: number) => {
      throw new Error("exit should not be reached after runtime.error");
    },
  };

  return {
    runtimeWithCapture,
    readCapturedJson: () => capturedJson,
  };
}

export function readOnboardFirstMockCall(mock: unknown, label: string): unknown[] {
  const calls = (mock as MockWithCalls<unknown[]>).mock.calls;
  const call = calls[0];
  if (!call) {
    throw new Error(`Expected ${label} to be called`);
  }
  return call;
}

export function createOnboardTestConfigStore() {
  const configStore = new Map<string, AetherConfig>();

  function resolveConfigPath() {
    const override = process.env.AETHER_CONFIG_PATH?.trim();
    if (override) {
      return override;
    }
    const stateDir = process.env.AETHER_STATE_DIR?.trim();
    if (!stateDir) {
      throw new Error("AETHER_STATE_DIR must be set before config IO in this test");
    }
    return path.join(stateDir, "aether.json");
  }

  function readConfig(): AetherConfig {
    return configStore.get(resolveConfigPath()) ?? {};
  }

  function readSnapshot(): ConfigFileSnapshot {
    const config = configStore.get(resolveConfigPath()) ?? {};
    const exists = configStore.has(resolveConfigPath());
    return createConfigFileSnapshot({
      path: resolveConfigPath(),
      exists,
      raw: exists ? `${JSON.stringify(config, null, 2)}\n` : null,
      parsed: config,
      sourceConfigBeforeMigrations: config,
      sourceConfig: config,
      valid: true,
      runtimeConfig: config,
      ...(exists ? { hash: "test-config-hash" } : {}),
      issues: [],
      warnings: [],
      legacyIssues: [],
    });
  }

  return { configStore, resolveConfigPath, readConfig, readSnapshot };
}

export function createOnboardStateDirHarness(getTempHome: () => string | undefined) {
  async function withStateDir(
    prefix: string,
    run: (stateDir: string) => Promise<void>,
  ): Promise<void> {
    const tempHome = getTempHome();
    if (!tempHome) {
      throw new Error("temp home not initialized");
    }
    const stateDir = await fs.realpath(await fs.mkdtemp(path.join(tempHome, prefix)));
    setTestEnvValue("AETHER_STATE_DIR", stateDir);
    deleteTestEnvValue("AETHER_CONFIG_PATH");
    try {
      await run(stateDir);
    } finally {
      await fs.rm(stateDir, { recursive: true, force: true });
    }
  }

  return { withStateDir };
}

export function prepareOnboardGatewayTestEnv() {
  const snapshot = captureEnv([
    "HOME",
    "AETHER_STATE_DIR",
    "AETHER_CONFIG_PATH",
    "AETHER_SKIP_CHANNELS",
    "AETHER_SKIP_GMAIL_WATCHER",
    "AETHER_SKIP_CRON",
    "AETHER_SKIP_CANVAS_HOST",
    "AETHER_SKIP_BROWSER_CONTROL_SERVER",
    "AETHER_GATEWAY_TOKEN",
    "AETHER_GATEWAY_PASSWORD",
  ]);
  setTestEnvValue("AETHER_SKIP_CHANNELS", "1");
  setTestEnvValue("AETHER_SKIP_GMAIL_WATCHER", "1");
  setTestEnvValue("AETHER_SKIP_CRON", "1");
  setTestEnvValue("AETHER_SKIP_CANVAS_HOST", "1");
  setTestEnvValue("AETHER_SKIP_BROWSER_CONTROL_SERVER", "1");
  deleteTestEnvValue("AETHER_GATEWAY_TOKEN");
  deleteTestEnvValue("AETHER_GATEWAY_PASSWORD");
  return snapshot;
}

export function createOnboardLocalDaemonOptions(stateDir: string) {
  return {
    nonInteractive: true,
    mode: "local" as const,
    workspace: path.join(stateDir, "aether"),
    authChoice: "skip" as const,
    skipSkills: true,
    skipHealth: false,
    installDaemon: true,
    gatewayBind: "loopback" as const,
  };
}

export async function runOnboardLocalDaemonSetup(params: {
  runSetup: RunNonInteractiveSetup;
  stateDir: string;
  runtime: RuntimeEnv;
}) {
  await params.runSetup(createOnboardLocalDaemonOptions(params.stateDir), params.runtime);
}

export async function expectOnboardLocalJsonSetupFailure(params: {
  runSetup: RunNonInteractiveSetup;
  stateDir: string;
  runtime: RuntimeEnv;
}) {
  await expect(
    params.runSetup(
      {
        ...createOnboardLocalDaemonOptions(params.stateDir),
        json: true,
      },
      params.runtime,
    ),
  ).rejects.toThrow("exit should not be reached after runtime.error");
}

export function createOnboardGatewayTimeoutCapture() {
  let capturedDeadlineMs: number | undefined;
  let capturedProbeTimeoutMs: number | undefined;
  const mock = vi.fn(
    async (params: {
      url: string;
      token?: string;
      password?: string;
      deadlineMs?: number;
      probeTimeoutMs?: number;
    }) => {
      capturedDeadlineMs = params.deadlineMs;
      capturedProbeTimeoutMs = params.probeTimeoutMs;
      return { ok: true };
    },
  );
  return {
    mock,
    get deadlineMs() {
      return capturedDeadlineMs;
    },
    get probeTimeoutMs() {
      return capturedProbeTimeoutMs;
    },
  };
}

export async function mockOnboardingAgent(params: {
  config: AetherConfig;
  baseConfig?: AetherConfig;
  workspace: string;
}) {
  const roster = listAgentEntries(params.config);
  const existing = roster.find((entry) => entry.default === true) ?? roster[0];
  if (existing) {
    return {
      config: params.config,
      configBase: params.baseConfig ?? params.config,
      agentId: existing.id,
      bootstrapPending: false,
    };
  }
  return {
    configBase: params.baseConfig ?? params.config,
    config: {
      ...params.config,
      agents: {
        ...params.config.agents,
        entries: { main: { name: "main", workspace: params.workspace, default: true } },
      },
    },
    agentId: "main",
    bootstrapPending: true,
  };
}
