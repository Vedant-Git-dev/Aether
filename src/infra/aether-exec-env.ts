/** Process env key that marks child commands as launched by the Aether CLI. */
export const AETHER_CLI_ENV_VAR = "AETHER_CLI";

/** Stable marker value used for Aether-launched subprocess detection. */
const AETHER_CLI_ENV_VALUE = "1";

/** Returns a cloned env object with the Aether CLI marker set. */
export function markAetherExecEnv<T extends Record<string, string | undefined>>(
  /** Source environment to clone before adding the subprocess marker. */
  env: T,
): T {
  return {
    ...env,
    [AETHER_CLI_ENV_VAR]: AETHER_CLI_ENV_VALUE,
  };
}

/** Mutates an existing process env object so current-process children inherit the marker. */
export function ensureAetherExecMarkerOnProcess(
  /** Process env object to mutate; defaults to the current process environment. */
  env: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  env[AETHER_CLI_ENV_VAR] = AETHER_CLI_ENV_VALUE;
  return env;
}
