// Formats Aether CLI command snippets for chat-facing command responses.
import { resolveCurrentAetherCliInvocation } from "../../infra/aether-cli-invocation.js";

const TEST_RUNNER_ENV_PREFIXES = ["VITEST_", "AETHER_VITEST_"];

function quoteShellArg(value: string): string {
  if (process.platform === "win32") {
    return `'${value.replaceAll("'", "''")}'`;
  }
  return `'${value.replaceAll("'", "'\\''")}'`;
}

/** Prepares one CLI command and its source context before exec approval or dispatch. */
export function buildCurrentAetherCliExecRequest(
  args: string[],
  env: NodeJS.ProcessEnv = process.env,
) {
  const invocation = resolveCurrentAetherCliInvocation(args);
  const argv = [invocation.command, ...invocation.args];
  const overrides: Record<string, string> = { ...invocation.env };
  for (const key of Object.keys(env)) {
    if (key === "VITEST" || TEST_RUNNER_ENV_PREFIXES.some((prefix) => key.startsWith(prefix))) {
      overrides[key] = "";
    }
  }
  return {
    argv,
    command: argv.map(quoteShellArg).join(" "),
    env: Object.keys(overrides).length > 0 ? overrides : undefined,
  };
}
