// Whatsapp plugin module maintains bounded chat/contact/message rosters.
// Baileys 7 has no built-in store, so the inbound listener feeds this in-memory
// projection from socket events. The read-only roster agent tools query it.
import type { Chat, ChatUpdate, Contact, WAMessage } from "baileys";
import { extractText } from "./inbound/extract.js";
import type { WhatsAppSocketListen } from "./inbound/lifecycle.js";

const MAX_ROSTER_CHATS = 1_000;
const MAX_ROSTER_CONTACTS = 5_000;
const MAX_ROSTER_MESSAGES_PER_CHAT = 200;
const MAX_ROSTER_MESSAGES_TOTAL = 5_000;
const MAX_ROSTER_MESSAGE_CHARS = 2_000;
const MAX_ROSTER_PREVIEW_CHARS = 160;

export type WhatsAppRosterChatKind = "dm" | "group";

export type WhatsAppRosterChat = {
  jid: string;
  kind: WhatsAppRosterChatKind;
  name: string | undefined;
  pushName: string | undefined;
  lastMessageAt: number | undefined;
  lastMessagePreview: string | undefined;
  unreadCount: number;
};

export type WhatsAppRosterContact = {
  jid: string;
  name: string | undefined;
  notify: string | undefined;
  phone: string | undefined;
};

export type WhatsAppRosterMessage = {
  id: string;
  jid: string;
  fromMe: boolean;
  senderJid: string | undefined;
  senderName: string | undefined;
  body: string | undefined;
  timestamp: number | undefined;
};

export type WhatsAppRosterChatView = {
  jid: string;
  kind: WhatsAppRosterChatKind;
  name: string;
  phone: string | undefined;
  lastMessageAt: number | undefined;
  lastMessagePreview: string | undefined;
  unreadCount: number;
};

export type WhatsAppRosterContactView = {
  jid: string;
  name: string | undefined;
  phone: string | undefined;
};

export type WhatsAppRosterChatMatch = {
  jid: string;
  name: string;
  kind: WhatsAppRosterChatKind;
};

type WhatsAppRosterState = {
  chats: Map<string, WhatsAppRosterChat>;
  contacts: Map<string, WhatsAppRosterContact>;
  messages: Map<string, WhatsAppRosterMessage[]>;
  messageCount: number;
};

const rosterStatesByAccount = new Map<string, WhatsAppRosterState>();

function isSkippableRosterJid(jid: string): boolean {
  return /@(broadcast|newsletter)$/i.test(jid);
}

function resolveRosterChatKind(jid: string): WhatsAppRosterChatKind {
  return /@g\.us$/i.test(jid) ? "group" : "dm";
}

function normalizeRosterTimestamp(value: unknown): number | undefined {
  // Baileys/protobuf timestamps arrive as numbers or Long objects (toNumber()).
  let numeric: number;
  if (typeof value === "number") {
    numeric = value;
  } else if (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { toNumber?: unknown }).toNumber === "function"
  ) {
    numeric = (value as { toNumber: () => number }).toNumber();
  } else if (value instanceof Date) {
    numeric = value.getTime();
  } else {
    return undefined;
  }
  if (!Number.isFinite(numeric) || numeric <= 0) {
    return undefined;
  }
  // WhatsApp protocol timestamps are unix seconds; some local paths already use ms.
  return Math.round(numeric < 1e12 ? numeric * 1_000 : numeric);
}

function resolveRosterDisplayText(text: string | undefined, maxChars: number): string | undefined {
  const trimmed = text?.trim();
  if (!trimmed) {
    return undefined;
  }
  const flattened = trimmed.replaceAll(/\s+/g, " ");
  return flattened.length > maxChars ? `${flattened.slice(0, maxChars)}…` : flattened;
}

function boundRosterMap<T>(map: Map<string, T>, maxEntries: number): void {
  while (map.size > maxEntries) {
    const oldest = map.keys().next();
    if (oldest.done) {
      break;
    }
    map.delete(oldest.value);
  }
}

/** Reinserts an entry so map iteration order tracks most-recent-activity-first eviction. */
function refreshRosterMapOrder<T>(map: Map<string, T>, key: string, value: T): void {
  map.delete(key);
  map.set(key, value);
}

function getOrCreateWhatsAppRosterState(accountId: string): WhatsAppRosterState {
  let state = rosterStatesByAccount.get(accountId);
  if (!state) {
    state = {
      chats: new Map(),
      contacts: new Map(),
      messages: new Map(),
      messageCount: 0,
    };
    rosterStatesByAccount.set(accountId, state);
  }
  return state;
}

/** Returns the roster recorded for an account, or undefined when no listener ever attached. */
export function getWhatsAppRosterState(accountId: string): WhatsAppRosterState | undefined {
  return rosterStatesByAccount.get(accountId);
}

function resolveOptionalTrimmedString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function noteRosterChatUpsert(state: WhatsAppRosterState, chats: Chat[]): void {
  for (const chat of chats) {
    const jid = typeof chat.id === "string" ? chat.id : undefined;
    if (!jid || isSkippableRosterJid(jid)) {
      continue;
    }
    const existing = state.chats.get(jid);
    const timestamp =
      normalizeRosterTimestamp(chat.lastMessageRecvTimestamp) ??
      normalizeRosterTimestamp(chat.lastMsgTimestamp);
    // Chat recency only moves forward so late backfill cannot reorder the list.
    const lastMessageAt =
      timestamp === undefined
        ? existing?.lastMessageAt
        : Math.max(timestamp, existing?.lastMessageAt ?? 0);
    refreshRosterMapOrder(state.chats, jid, {
      jid,
      kind: resolveRosterChatKind(jid),
      name: resolveOptionalTrimmedString(chat.name) ?? existing?.name,
      pushName: existing?.pushName,
      lastMessageAt,
      lastMessagePreview: existing?.lastMessagePreview,
      unreadCount:
        typeof chat.unreadCount === "number" && chat.unreadCount >= 0
          ? chat.unreadCount
          : (existing?.unreadCount ?? 0),
    });
  }
  boundRosterMap(state.chats, MAX_ROSTER_CHATS);
}

function noteRosterChatUpdate(state: WhatsAppRosterState, updates: ChatUpdate[]): void {
  for (const update of updates) {
    const jid = typeof update.id === "string" ? update.id : undefined;
    if (!jid || isSkippableRosterJid(jid)) {
      continue;
    }
    const existing = state.chats.get(jid);
    if (!existing) {
      const name = resolveOptionalTrimmedString(update.name);
      if (name) {
        refreshRosterMapOrder(state.chats, jid, {
          jid,
          kind: resolveRosterChatKind(jid),
          name,
          pushName: undefined,
          lastMessageAt:
            normalizeRosterTimestamp(update.lastMessageRecvTimestamp) ??
            normalizeRosterTimestamp(update.lastMsgTimestamp),
          lastMessagePreview: undefined,
          unreadCount:
            typeof update.unreadCount === "number" && update.unreadCount >= 0
              ? update.unreadCount
              : 0,
        });
      }
      continue;
    }
    const timestamp =
      normalizeRosterTimestamp(update.lastMessageRecvTimestamp) ??
      normalizeRosterTimestamp(update.lastMsgTimestamp);
    const lastMessageAt =
      timestamp === undefined
        ? existing.lastMessageAt
        : Math.max(timestamp, existing.lastMessageAt ?? 0);
    refreshRosterMapOrder(state.chats, jid, {
      ...existing,
      name: resolveOptionalTrimmedString(update.name) ?? existing.name,
      ...(lastMessageAt !== undefined ? { lastMessageAt } : {}),
      ...(typeof update.unreadCount === "number" && update.unreadCount >= 0
        ? { unreadCount: update.unreadCount }
        : {}),
    });
  }
  boundRosterMap(state.chats, MAX_ROSTER_CHATS);
}

function noteRosterChatDelete(state: WhatsAppRosterState, jids: string[]): void {
  for (const jid of jids) {
    const chatMessages = state.messages.get(jid);
    if (chatMessages) {
      state.messageCount -= chatMessages.length;
      state.messages.delete(jid);
    }
    state.chats.delete(jid);
  }
}

function noteRosterContacts(state: WhatsAppRosterState, contacts: Array<Partial<Contact>>): void {
  for (const contact of contacts) {
    const jid = typeof contact.id === "string" ? contact.id : undefined;
    if (!jid) {
      continue;
    }
    const existing = state.contacts.get(jid);
    refreshRosterMapOrder(state.contacts, jid, {
      jid,
      name: resolveOptionalTrimmedString(contact.name) ?? existing?.name,
      notify: resolveOptionalTrimmedString(contact.notify) ?? existing?.notify,
      phone: resolveOptionalTrimmedString(contact.phoneNumber) ?? existing?.phone,
    });
  }
  boundRosterMap(state.contacts, MAX_ROSTER_CONTACTS);
}

function evictRosterMessageOverflow(state: WhatsAppRosterState): void {
  while (state.messageCount > MAX_ROSTER_MESSAGES_TOTAL) {
    let evicted = false;
    for (const [jid, chatMessages] of state.messages) {
      if (chatMessages.length > 0) {
        chatMessages.shift();
        state.messageCount -= 1;
        if (chatMessages.length === 0) {
          state.messages.delete(jid);
        }
        evicted = true;
        break;
      }
    }
    if (!evicted) {
      break;
    }
  }
}

function noteRosterMessage(state: WhatsAppRosterState, message: WAMessage): void {
  const jid = typeof message.key?.remoteJid === "string" ? message.key.remoteJid : undefined;
  const id = typeof message.key?.id === "string" ? message.key.id : undefined;
  if (!jid || !id || isSkippableRosterJid(jid)) {
    return;
  }
  const fromMe = message.key?.fromMe === true;
  const body = extractText(message.message ?? undefined);
  const timestamp = normalizeRosterTimestamp(message.messageTimestamp);
  const senderJid =
    !fromMe && typeof message.key?.participant === "string" && message.key.participant
      ? message.key.participant
      : undefined;
  const senderName = fromMe ? undefined : resolveOptionalTrimmedString(message.pushName);

  const chatMessages = state.messages.get(jid) ?? [];
  const entry: WhatsAppRosterMessage = {
    id,
    jid,
    fromMe,
    senderJid,
    senderName,
    body: resolveRosterDisplayText(body, MAX_ROSTER_MESSAGE_CHARS),
    timestamp,
  };
  const existingIndex = chatMessages.findIndex((candidate) => candidate.id === id);
  if (existingIndex >= 0) {
    chatMessages[existingIndex] = entry;
  } else {
    chatMessages.push(entry);
    state.messageCount += 1;
    if (chatMessages.length > MAX_ROSTER_MESSAGES_PER_CHAT) {
      chatMessages.shift();
      state.messageCount -= 1;
    }
  }
  refreshRosterMapOrder(state.messages, jid, chatMessages);
  evictRosterMessageOverflow(state);

  // Keep the chat list current even for chats not surfaced by chats.upsert yet.
  const existingChat = state.chats.get(jid);
  const lastMessageAt =
    timestamp === undefined
      ? existingChat?.lastMessageAt
      : Math.max(timestamp, existingChat?.lastMessageAt ?? 0);
  const advancesActivity =
    timestamp === undefined || timestamp >= (existingChat?.lastMessageAt ?? 0);
  const preview = resolveRosterDisplayText(body, MAX_ROSTER_PREVIEW_CHARS);
  refreshRosterMapOrder(state.chats, jid, {
    jid,
    kind: resolveRosterChatKind(jid),
    name: existingChat?.name,
    pushName: senderName ?? existingChat?.pushName,
    lastMessageAt,
    lastMessagePreview:
      (advancesActivity || !existingChat?.lastMessagePreview ? preview : undefined) ??
      existingChat?.lastMessagePreview,
    unreadCount: existingChat?.unreadCount ?? 0,
  });
  boundRosterMap(state.chats, MAX_ROSTER_CHATS);
}

/**
 * Feeds the account's bounded roster from socket events. Safe to attach once per
 * inbound listener; roster state survives reconnects for the same account. The
 * returned detach stops updates when the listener closes.
 */
export function attachWhatsAppRosterTracker(
  accountId: string,
  params: { listen: WhatsAppSocketListen },
): () => void {
  const { listen } = params;
  const state = getOrCreateWhatsAppRosterState(accountId);
  const detachChatsUpsert = listen("chats.upsert", (chats) => {
    noteRosterChatUpsert(state, chats);
  });
  const detachChatsUpdate = listen("chats.update", (updates) => {
    noteRosterChatUpdate(state, updates);
  });
  const detachChatsDelete = listen("chats.delete", (jids) => {
    noteRosterChatDelete(state, jids);
  });
  const detachContactsUpsert = listen("contacts.upsert", (contacts) => {
    noteRosterContacts(state, contacts);
  });
  const detachContactsUpdate = listen("contacts.update", (contacts) => {
    noteRosterContacts(state, contacts);
  });
  const detachMessagesUpsert = listen("messages.upsert", ({ messages }) => {
    for (const message of messages) {
      noteRosterMessage(state, message);
    }
  });
  return () => {
    detachChatsUpsert();
    detachChatsUpdate();
    detachChatsDelete();
    detachContactsUpsert();
    detachContactsUpdate();
    detachMessagesUpsert();
  };
}

function resolveRosterPhoneDigits(jid: string): string | undefined {
  return /^(\d+)(?::\d+)?@/.exec(jid)?.[1];
}

function resolveWhatsAppRosterChatName(
  state: WhatsAppRosterState,
  chat: WhatsAppRosterChat,
): string {
  if (chat.kind === "group") {
    return chat.name ?? chat.jid;
  }
  const contact = state.contacts.get(chat.jid);
  return (
    contact?.name ??
    contact?.notify ??
    chat.name ??
    chat.pushName ??
    resolveRosterPhoneDigits(chat.jid) ??
    chat.jid
  );
}

/** Lists chats most recently active first; undefined means no roster exists yet. */
export function listWhatsAppRosterChats(
  accountId: string,
  limit: number,
): WhatsAppRosterChatView[] | undefined {
  const state = getWhatsAppRosterState(accountId);
  if (!state) {
    return undefined;
  }
  const sorted = [...state.chats.values()].sort(
    (left, right) => (right.lastMessageAt ?? 0) - (left.lastMessageAt ?? 0),
  );
  return sorted.slice(0, limit).map((chat) => ({
    jid: chat.jid,
    kind: chat.kind,
    name: resolveWhatsAppRosterChatName(state, chat),
    phone: chat.kind === "dm" ? resolveRosterPhoneDigits(chat.jid) : undefined,
    lastMessageAt: chat.lastMessageAt,
    lastMessagePreview: chat.lastMessagePreview,
    unreadCount: chat.unreadCount,
  }));
}

/** Lists contacts, optionally filtered by a case-insensitive name/phone/JID substring. */
export function listWhatsAppRosterContacts(
  accountId: string,
  params: { query: string | undefined; limit: number },
): WhatsAppRosterContactView[] | undefined {
  const state = getWhatsAppRosterState(accountId);
  if (!state) {
    return undefined;
  }
  const needle = params.query?.trim().toLowerCase();
  const contacts = [...state.contacts.values()];
  const filtered = needle
    ? contacts.filter((contact) =>
        [contact.jid, contact.name, contact.notify, contact.phone].some(
          (value) => typeof value === "string" && value.toLowerCase().includes(needle),
        ),
      )
    : contacts;
  return filtered.slice(0, params.limit).map((contact) => ({
    jid: contact.jid,
    name: contact.name ?? contact.notify,
    phone: contact.phone ?? resolveRosterPhoneDigits(contact.jid),
  }));
}

/**
 * Returns the chat's most recent messages, oldest first, capped by limit.
 * An empty array means the chat is known but nothing was recorded while attached.
 */
export function listWhatsAppRosterHistory(
  accountId: string,
  jid: string,
  limit: number,
): WhatsAppRosterMessage[] | undefined {
  const state = getWhatsAppRosterState(accountId);
  if (!state) {
    return undefined;
  }
  const chatMessages = state.messages.get(jid);
  if (!chatMessages || chatMessages.length === 0) {
    return [];
  }
  const sorted = [...chatMessages].sort(
    (left, right) => (left.timestamp ?? 0) - (right.timestamp ?? 0),
  );
  return sorted.slice(-limit);
}

/** Resolves a fuzzy chat/contact name to matching roster entries for disambiguation. */
export function resolveWhatsAppRosterChatByName(
  accountId: string,
  query: string,
): WhatsAppRosterChatMatch[] | undefined {
  const state = getWhatsAppRosterState(accountId);
  if (!state) {
    return undefined;
  }
  const needle = query.trim().toLowerCase();
  const matches = new Map<string, WhatsAppRosterChatMatch>();
  for (const chat of state.chats.values()) {
    const name = resolveWhatsAppRosterChatName(state, chat);
    if (name.toLowerCase().includes(needle)) {
      matches.set(chat.jid, { jid: chat.jid, name, kind: chat.kind });
    }
  }
  for (const contact of state.contacts.values()) {
    const displayName = contact.name ?? contact.notify ?? contact.jid;
    if (displayName.toLowerCase().includes(needle) && !matches.has(contact.jid)) {
      matches.set(contact.jid, { jid: contact.jid, name: displayName, kind: "dm" });
    }
  }
  return [...matches.values()];
}
