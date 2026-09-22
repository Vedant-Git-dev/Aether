// Xiaomi setup module handles plugin onboarding behavior.
import {
  createDefaultModelsPresetAppliers,
  createDefaultModelsConnectionPresetAppliers,
  type AetherConfig,
} from "aether/plugin-sdk/provider-onboard";
import {
  buildXiaomiProvider,
  buildXiaomiTokenPlanProvider,
  resolveXiaomiTokenPlanBaseUrl,
  XIAOMI_DEFAULT_MODEL_ID,
  XIAOMI_PROVIDER_ID,
  XIAOMI_TOKEN_PLAN_DEFAULT_MODEL_ID,
  XIAOMI_TOKEN_PLAN_PROVIDER_ID,
  type XiaomiTokenPlanRegion,
} from "./provider-catalog.js";

export const XIAOMI_DEFAULT_MODEL_REF = `${XIAOMI_PROVIDER_ID}/${XIAOMI_DEFAULT_MODEL_ID}`;
export const XIAOMI_TOKEN_PLAN_DEFAULT_MODEL_REF = `${XIAOMI_TOKEN_PLAN_PROVIDER_ID}/${XIAOMI_TOKEN_PLAN_DEFAULT_MODEL_ID}`;

const xiaomiPreset = {
  primaryModelRef: XIAOMI_DEFAULT_MODEL_REF,
  resolveParams: () => {
    const defaultProvider = buildXiaomiProvider();
    return {
      providerId: XIAOMI_PROVIDER_ID,
      api: defaultProvider.api ?? "openai-completions",
      baseUrl: defaultProvider.baseUrl,
      defaultModels: () => defaultProvider.models ?? [],
      defaultModelId: XIAOMI_DEFAULT_MODEL_ID,
      aliases: [{ modelRef: XIAOMI_DEFAULT_MODEL_REF, alias: "Xiaomi" }],
    };
  },
} satisfies Parameters<typeof createDefaultModelsConnectionPresetAppliers<[]>>[0];

export const { applyConfig: applyXiaomiConfig, applyProviderConfig: applyXiaomiProviderConfig } =
  createDefaultModelsPresetAppliers(xiaomiPreset);
export const { applyConfig: applyXiaomiConnectionConfig } =
  createDefaultModelsConnectionPresetAppliers(xiaomiPreset);

const xiaomiTokenPlanPresetAppliers = createDefaultModelsPresetAppliers<[]>({
  primaryModelRef: XIAOMI_TOKEN_PLAN_DEFAULT_MODEL_REF,
  resolveParams: (cfg) => {
    const defaultProvider = buildXiaomiTokenPlanProvider();
    return {
      providerId: XIAOMI_TOKEN_PLAN_PROVIDER_ID,
      api: defaultProvider.api ?? "openai-completions",
      baseUrl: defaultProvider.baseUrl,
      defaultModels: cfg.models?.mode === "replace" ? (defaultProvider.models ?? []) : [],
      defaultModelId: XIAOMI_TOKEN_PLAN_DEFAULT_MODEL_ID,
      aliases: (() => {
        const defaultModel = defaultProvider.models?.find(
          (m) => m.id === XIAOMI_TOKEN_PLAN_DEFAULT_MODEL_ID,
        );
        return [
          {
            modelRef: XIAOMI_TOKEN_PLAN_DEFAULT_MODEL_REF,
            alias: defaultModel?.name ?? "MiMo V2.5 Pro",
          },
        ];
      })(),
    };
  },
});

function withProviderBaseUrl(
  cfg: AetherConfig,
  providerId: string,
  baseUrl: string,
): AetherConfig {
  const providers: Record<string, unknown> = {
    ...cfg.models?.providers,
    [providerId]: {
      ...cfg.models?.providers?.[providerId],
      baseUrl,
    },
  };
  return {
    ...cfg,
    models: {
      ...cfg.models,
      providers,
    },
  } as AetherConfig;
}

export function applyXiaomiTokenPlanConfig(
  cfg: AetherConfig,
  region: XiaomiTokenPlanRegion,
): AetherConfig {
  return withProviderBaseUrl(
    xiaomiTokenPlanPresetAppliers.applyConfig(cfg),
    XIAOMI_TOKEN_PLAN_PROVIDER_ID,
    resolveXiaomiTokenPlanBaseUrl(region),
  );
}
