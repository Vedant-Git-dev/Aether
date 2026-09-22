/**
 * Remote non-interactive onboarding orchestration.
 *
 * It writes gateway.remote config without local gateway setup, preserving the
 * same config commit path as local onboarding.
 */
import { normalizeOptionalString } from "@aether/normalization-core/string-coerce";
import { formatCliCommand } from "../../cli/command-format.js";
import { logConfigUpdated } from "../../config/logging.js";
import type { AetherConfig } from "../../config/types.aether.js";
import { type RuntimeEnv, writeRuntimeJson } from "../../runtime.js";
import { createGatewayEnvSecretRef } from "../../secrets/ref-contract.js";
import { applySkipBootstrapConfig } from "../onboard-config.js";
import { applyWizardMetadata } from "../onboard-helpers.js";
import type { OnboardOptions } from "../onboard-types.js";
import { commitNonInteractiveOnboardConfig } from "./config-write.js";

/** Runs non-interactive setup for clients that connect to an existing remote gateway. */
export async function runNonInteractiveRemoteSetup(params: {
  opts: OnboardOptions;
  runtime: RuntimeEnv;
  baseConfig: AetherConfig;
  baseHash?: string;
}) {
  const { opts, runtime, baseConfig, baseHash } = params;
  const mode = "remote" as const;

  const remoteUrl = normalizeOptionalString(opts.remoteUrl);
  if (!remoteUrl) {
    // Remote mode cannot infer a target gateway; fail before writing partial
    // remote config that would leave status/agent commands misconfigured.
    runtime.error(
      `Missing --remote-url for remote mode. Example: ${formatCliCommand("aether awaken --non-interactive --mode remote --remote-url ws://127.0.0.1:3000")}.`,
    );
    runtime.exit(1);
    return;
  }
  const remoteToken = normalizeOptionalString(opts.remoteToken);
  const remotePassword = normalizeOptionalString(opts.remotePassword);
  for (const [flag, input, normalized] of [
    ["--remote-token", opts.remoteToken, remoteToken],
    ["--remote-password", opts.remotePassword, remotePassword],
  ] as const) {
    if (input !== undefined && !normalized) {
      runtime.error(`Invalid ${flag}: value cannot be empty.`);
      runtime.exit(1);
      return;
    }
  }
  if (remoteToken && remotePassword) {
    runtime.error("Use either --remote-token or --remote-password, not both.");
    runtime.exit(1);
    return;
  }
  const existingRemote = baseConfig.gateway?.remote;
  const remoteUrlChanged = normalizeOptionalString(existingRemote?.url) !== remoteUrl;
  // A remote block belongs to one endpoint. Reusing it for a different URL can
  // send old credentials or keep routing through the old SSH target.
  const preservedRemote = remoteUrlChanged ? {} : { ...existingRemote };
  if (remoteToken) {
    delete preservedRemote.password;
  }
  if (remotePassword) {
    delete preservedRemote.token;
  }

  let nextConfig: AetherConfig = {
    ...baseConfig,
    gateway: {
      ...baseConfig.gateway,
      mode: "remote",
      remote: {
        ...preservedRemote,
        url: remoteUrl,
        ...(remoteToken
          ? {
              token:
                opts.secretInputMode === "ref"
                  ? createGatewayEnvSecretRef(baseConfig, "AETHER_GATEWAY_TOKEN")
                  : remoteToken,
            }
          : {}),
        ...(remotePassword
          ? {
              password:
                opts.secretInputMode === "ref"
                  ? createGatewayEnvSecretRef(baseConfig, "AETHER_GATEWAY_PASSWORD")
                  : remotePassword,
            }
          : {}),
      },
    },
  };
  if (opts.skipBootstrap) {
    nextConfig = applySkipBootstrapConfig(nextConfig);
  }
  nextConfig = applyWizardMetadata(nextConfig, { command: "onboard", mode });
  await commitNonInteractiveOnboardConfig({
    nextConfig,
    baseConfig,
    baseHash,
    reset: opts.reset,
  });
  logConfigUpdated(runtime);

  const payload = {
    mode,
    remoteUrl,
    auth: nextConfig.gateway?.remote?.token
      ? "token"
      : nextConfig.gateway?.remote?.password
        ? ["pass", "word"].join("")
        : "none",
  };
  if (opts.json) {
    writeRuntimeJson(runtime, payload);
  } else {
    runtime.log(`Remote gateway: ${remoteUrl}`);
    runtime.log(`Auth: ${payload.auth}`);
    runtime.log(
      `Tip: run \`${formatCliCommand("aether tune --section web")}\` to store your Brave API key for web_search. Docs: https://docs.aether.ai/tools/web`,
    );
  }
}
