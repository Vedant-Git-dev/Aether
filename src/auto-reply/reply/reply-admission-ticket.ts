import { normalizeStringifiedEntries } from "@aether/normalization-core/string-coerce";
import {
  createKeyedFifoLeaseRegistry,
  type KeyedFifoLease,
} from "../../shared/keyed-fifo-lease.js";

export const REPLY_ADMISSION_TICKET = Symbol("aether.replyAdmissionTicket");
type ReplyAdmissionTicket = KeyedFifoLease;
export type ReplyOptionsWithAdmissionTicket = {
  [REPLY_ADMISSION_TICKET]?: ReplyAdmissionTicket;
};

const replyAdmissionTickets = createKeyedFifoLeaseRegistry(
  Symbol.for("aether.replyAdmissionTickets"),
);

/** Briefly orders queue publication across a command's source and target sessions. */
export function reserveReplyAdmissionTicket(
  sessionKeys: ReadonlyArray<string | undefined>,
): ReplyAdmissionTicket | undefined {
  return replyAdmissionTickets.reserve(normalizeStringifiedEntries(sessionKeys));
}
