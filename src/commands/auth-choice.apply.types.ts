// Shared types for applying auth-choice selections during onboarding and agent setup.
import type { AetherConfig } from "../config/types.aether.js";
import type { ProviderAuthResult } from "../plugins/types.js";
import type { RuntimeEnv } from "../runtime.js";
import type { WizardPrompter } from "../wizard/prompts.js";
import type { AuthChoice, OnboardOptions } from "./onboard-types.js";

export type ApplyAuthChoiceParams = {
  authChoice: AuthChoice;
  config: AetherConfig;
  env?: NodeJS.ProcessEnv;
  prompter: WizardPrompter;
  runtime: RuntimeEnv;
  agentDir?: string;
  workspaceDir?: string;
  setDefaultModel: boolean;
  preserveExistingDefaultModel?: boolean;
  agentId?: string;
  opts?: Partial<OnboardOptions>;
};

export type ApplyAuthChoiceResult = {
  config: AetherConfig;
  agentModelOverride?: string;
  retrySelection?: boolean;
};

export type PreparedAuthChoiceResult = ApplyAuthChoiceResult & {
  authProfiles: ProviderAuthResult["profiles"];
  persistAuthProfiles: (profiles?: ProviderAuthResult["profiles"]) => Promise<void>;
};
