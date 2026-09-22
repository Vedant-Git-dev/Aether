// WhatsApp plugin read-only roster tools expose the linked account's chats,
// contacts, and recent history to the agent.
import { normalizeAccountId } from "aether/plugin-sdk/account-id";
import {
  createActionGate,
  optionalPositiveIntegerSchema,
  readPositiveIntegerParam,
  readStringParam,
} from "aether/plugin-sdk/channel-actions";
import type {
  AnyAgentTool,
  AetherPluginApi,
  AetherPluginToolContext,
} from "aether/plugin-sdk/core";
import { jsonResult } from "aether/plugin-sdk/tool-results";
import { Type } from "typebox";
import { getWhatsAppConnectionController } from "./connection-controller-runtime-context.js";
import {
  listWhatsAppRosterChats,
  listWhatsAppRosterContacts,
  listWhatsAppRosterHistory,
  resolveWhatsAppRosterChatByName,
} from "./roster-state.js";
import { toWhatsappJid } from "./text-runtime.js";

const DEFAULT_CHATS_LIMIT = 25;
const MAX_CHATS_LIMIT = 100;
const DEFAULT_CONTACTS_LIMIT = 50;
const MAX_CONTACTS_LIMIT = 100;
const DEFAULT_HISTORY_LIMIT = 20;
const MAX_HISTORY_LIMIT = 100;
const MAX_CONTACT_QUERY_CHARS = 128;
const MAX_CHAT_REF_CHARS = 256;
const MAX_MATCHED_CHATS_HINT = 10;
const PHONE_LIKE_RE = /^\+?\d[\d\s().-]{6,}$/;

const ROSTER_UNAVAILABLE_MESSAGE =
  "No WhatsApp chats or contacts are recorded yet for this account. Connect the WhatsApp listener first; roster tools only see chats observed while the listener was attached.";

const WhatsAppChatsToolSchema = Type.Object(
  {
    limit: optionalPositiveIntegerSchema({
      maximum: MAX_CHATS_LIMIT,
      description: "Maximum chats to return (most recently active first)",
    }),
  },
  { additionalProperties: false },
);

const WhatsAppContactsToolSchema = Type.Object(
  {
    query: Type.Optional(
      Type.String({
        description: "Optional case-insensitive filter on contact name, phone, or JID",
        maxLength: MAX_CONTACT_QUERY_CHARS,
      }),
    ),
    limit: optionalPositiveIntegerSchema({
      maximum: MAX_CONTACTS_LIMIT,
      description: "Maximum contacts to return",
    }),
  },
  { additionalProperties: false },
);

const WhatsAppHistoryToolSchema = Type.Object(
  {
    chat: Type.String({
      description: "Chat to read: a WhatsApp JID, a phone number, or a unique chat/contact name",
      maxLength: MAX_CHAT_REF_CHARS,
    }),
    limit: optionalPositiveIntegerSchema({
      maximum: MAX_HISTORY_LIMIT,
      description: "Maximum messages to return (oldest first, most recent kept)",
    }),
  },
  { additionalProperties: false },
);

function resolveRuntimeConfig(api: AetherPluginApi, context: AetherPluginToolContext) {
  return context.getRuntimeConfig?.() ?? context.runtimeConfig ?? context.config ?? api.config;
}

function createRosterToolContext(api: AetherPluginApi, context: AetherPluginToolContext) {
  const cfg = resolveRuntimeConfig(api, context);
  const isActionEnabled = createActionGate(cfg.channels?.whatsapp?.actions);
  if (!isActionEnabled("roster", true)) {
    return null;
  }
  const accountId = normalizeAccountId(context.agentAccountId);
  const connected = Boolean(getWhatsAppConnectionController(accountId)?.getCurrentSock());
  return { accountId, connected };
}

function createWhatsAppChatsTool(
  api: AetherPluginApi,
  context: AetherPluginToolContext,
): AnyAgentTool | null {
  const roster = createRosterToolContext(api, context);
  if (!roster) {
    return null;
  }
  const { accountId, connected } = roster;
  return {
    name: "whatsapp_chats",
    label: "WhatsApp Chats",
    description:
      "List recent WhatsApp chats for the linked account, most recently active first. Use whatsapp_history to read a chat's messages.",
    parameters: WhatsAppChatsToolSchema,
    async execute(_toolCallId, rawParams) {
      const limit =
        readPositiveIntegerParam(rawParams as Record<string, unknown>, "limit", {
          max: MAX_CHATS_LIMIT,
        }) ?? DEFAULT_CHATS_LIMIT;
      const chats = listWhatsAppRosterChats(accountId, limit);
      if (!chats) {
        throw new Error(ROSTER_UNAVAILABLE_MESSAGE);
      }
      return jsonResult({ accountId, connected, count: chats.length, chats });
    },
  };
}

function createWhatsAppContactsTool(
  api: AetherPluginApi,
  context: AetherPluginToolContext,
): AnyAgentTool | null {
  const roster = createRosterToolContext(api, context);
  if (!roster) {
    return null;
  }
  const { accountId, connected } = roster;
  return {
    name: "whatsapp_contacts",
    label: "WhatsApp Contacts",
    description:
      "Search the WhatsApp contacts observed for the linked account, optionally filtered by a case-insensitive name, phone, or JID substring.",
    parameters: WhatsAppContactsToolSchema,
    async execute(_toolCallId, rawParams) {
      const params = rawParams as Record<string, unknown>;
      const query = readStringParam(params, "query");
      const limit =
        readPositiveIntegerParam(params, "limit", {
          max: MAX_CONTACTS_LIMIT,
        }) ?? DEFAULT_CONTACTS_LIMIT;
      const contacts = listWhatsAppRosterContacts(accountId, { query, limit });
      if (!contacts) {
        throw new Error(ROSTER_UNAVAILABLE_MESSAGE);
      }
      return jsonResult({
        accountId,
        connected,
        query: query ?? null,
        count: contacts.length,
        contacts,
      });
    },
  };
}

function createWhatsAppHistoryTool(
  api: AetherPluginApi,
  context: AetherPluginToolContext,
): AnyAgentTool | null {
  const roster = createRosterToolContext(api, context);
  if (!roster) {
    return null;
  }
  const { accountId, connected } = roster;
  return {
    name: "whatsapp_history",
    label: "WhatsApp History",
    description:
      "Read recent WhatsApp messages for one chat, oldest first. History only covers messages observed while the WhatsApp listener was attached, so older messages may be missing.",
    parameters: WhatsAppHistoryToolSchema,
    async execute(_toolCallId, rawParams) {
      const params = rawParams as Record<string, unknown>;
      const chatRef = readStringParam(params, "chat", { required: true, label: "chat" });
      const limit =
        readPositiveIntegerParam(params, "limit", {
          max: MAX_HISTORY_LIMIT,
        }) ?? DEFAULT_HISTORY_LIMIT;

      let jid: string | null = null;
      if (chatRef.includes("@")) {
        jid = toWhatsappJid(chatRef);
      } else if (PHONE_LIKE_RE.test(chatRef)) {
        try {
          jid = toWhatsappJid(chatRef);
        } catch {
          jid = null;
        }
      }
      if (!jid) {
        const matches = resolveWhatsAppRosterChatByName(accountId, chatRef) ?? [];
        if (matches.length === 1) {
          jid = matches[0].jid;
        } else if (matches.length > 1) {
          return jsonResult({
            accountId,
            connected,
            chat: chatRef,
            matchedChats: matches.slice(0, MAX_MATCHED_CHATS_HINT),
            message: `Multiple chats match "${chatRef}"; call again with the jid of the intended chat.`,
          });
        }
      }
      if (!jid) {
        throw new Error(
          `No WhatsApp chat matched "${chatRef}"; list chats with whatsapp_chats first`,
        );
      }

      const messages = listWhatsAppRosterHistory(accountId, jid, limit);
      if (!messages) {
        throw new Error(ROSTER_UNAVAILABLE_MESSAGE);
      }
      return jsonResult({ accountId, connected, chat: jid, count: messages.length, messages });
    },
  };
}

export function registerWhatsAppRosterTools(api: AetherPluginApi): void {
  api.registerTool((context) => createWhatsAppChatsTool(api, context), {
    name: "whatsapp_chats",
  });
  api.registerTool((context) => createWhatsAppContactsTool(api, context), {
    name: "whatsapp_contacts",
  });
  api.registerTool((context) => createWhatsAppHistoryTool(api, context), {
    name: "whatsapp_history",
  });
}
