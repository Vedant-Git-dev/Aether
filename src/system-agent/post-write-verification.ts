// Validates config after an approved Aether write and asks for one repair.
import { isSystemAgentInferenceUnavailableError } from "./inference-error.js";

function unavailable(reason: string): string {
  return [
    `⚠ The write was applied, but post-write verification is unavailable: ${reason}.`,
    "Run `aether vitals --fix` on the machine running Aether, then verify the configuration before continuing.",
  ].join("\n");
}

export async function verifyConfigAfterSystemAgentWrite(
  resolveRepair: (message: string) => Promise<{ text: string }>,
): Promise<string | null> {
  let issuesText: string;
  try {
    const { readConfigFileSnapshot } = await import("../config/config.js");
    const snapshot = await readConfigFileSnapshot();
    if (!snapshot.exists) {
      return unavailable("aether.json was not found");
    }
    if (snapshot.valid) {
      return null;
    }
    const issues = (snapshot.issues ?? []).map(
      (issue: { path?: string; message: string }) =>
        `${issue.path ? `${issue.path}: ` : ""}${issue.message}`,
    );
    issuesText = issues.length > 0 ? issues.join("\n") : "unknown validation failure";
  } catch {
    return unavailable("aether.json could not be read");
  }
  const notice = `⚠ aether.json failed validation after that write:\n${issuesText}`;
  let recovery: { text: string };
  try {
    recovery = await resolveRepair(
      `[config-verify] The config file is now invalid:\n${issuesText}\nPropose one corrective command from the allowed list.`,
    );
  } catch (error) {
    if (!isSystemAgentInferenceUnavailableError(error)) {
      throw error;
    }
    return `${notice}\nThe write was applied, but inference could not propose a repair. Run \`aether vitals --fix\` on the machine running Aether, then try again.`;
  }
  return recovery.text
    ? `${notice}\n\n${recovery.text}`
    : `${notice}\nUse \`config schema <path>\` here to check the expected shape. Or, with Aether stopped, run \`aether vitals --fix\` on the machine running it.`;
}
