import { createLazyRuntimeModule } from "aether/plugin-sdk/lazy-runtime";
import type { MatrixClient } from "../sdk.js";
import { MATRIX_AETHER_FINALIZED_PREVIEW_KEY } from "../send/types.js";

export type MatrixDraftStreamHandle = ReturnType<
  typeof import("../draft-stream.js").createMatrixDraftStream
>;

export async function redactMatrixDraftEvent(
  client: MatrixClient,
  roomId: string,
  draftEventId: string,
): Promise<boolean> {
  return await client.redactEvent(roomId, draftEventId).then(
    () => true,
    () => false,
  );
}

export function buildMatrixFinalizedPreviewContent(): Record<string, unknown> {
  return { [MATRIX_AETHER_FINALIZED_PREVIEW_KEY]: true };
}

export const loadMatrixSendModule = createLazyRuntimeModule(() => import("../send.js"));

export const loadAcpBindingRuntime = createLazyRuntimeModule(
  () => import("aether/plugin-sdk/acp-binding-runtime"),
);

export const loadSessionBindingRuntime = createLazyRuntimeModule(
  () => import("aether/plugin-sdk/session-binding-runtime"),
);

export const loadMatrixReactionEvents = createLazyRuntimeModule(
  () => import("./reaction-events.js"),
);

export const loadMatrixDraftStream = createLazyRuntimeModule(() => import("../draft-stream.js"));

export async function matrixTextWouldActivateMentions(
  client: MatrixClient,
  text: string,
): Promise<boolean> {
  const { resolveMatrixMentionsForBody } = await loadMatrixSendModule();
  const mentions = await resolveMatrixMentionsForBody({ client, body: text });
  return mentions.room === true || (mentions.user_ids?.length ?? 0) > 0;
}
