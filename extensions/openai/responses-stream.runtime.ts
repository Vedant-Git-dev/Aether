import type { ProviderWrapStreamFnContext } from "aether/plugin-sdk/plugin-entry";
import { buildProviderStreamFamilyHooks } from "aether/plugin-sdk/provider-stream-family";
import { createOpenAINativeWebSearchWrapper } from "./native-web-search.js";

const { wrapStreamFn } = buildProviderStreamFamilyHooks("openai-responses-defaults");

export function wrapOpenAIResponsesStream(ctx: ProviderWrapStreamFnContext) {
  return createOpenAINativeWebSearchWrapper(wrapStreamFn?.(ctx) ?? ctx.streamFn, {
    config: ctx.config,
    agentId: ctx.agentId,
    nativeWebSearchAllowedByToolPolicy: ctx.nativeWebSearchAllowedByToolPolicy,
  });
}
