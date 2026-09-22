import { asOptionalRecord } from "@aether/normalization-core/record-coerce";
import { AETHER_RUNTIME_CONTEXT_CUSTOM_TYPE } from "../agents/internal-runtime-context.js";

export function isVisibleTranscriptRecord(value: unknown): value is Record<string, unknown> {
  const record = asOptionalRecord(value);
  return (
    Boolean(record?.message) ||
    record?.type === "compaction" ||
    record?.type === "reset" ||
    (record?.type === "custom_message" &&
      record.display === true &&
      record.customType !== AETHER_RUNTIME_CONTEXT_CUSTOM_TYPE)
  );
}
