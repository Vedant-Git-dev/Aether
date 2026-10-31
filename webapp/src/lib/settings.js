// Static vocabulary for the settings surface: section order, service faces,
// credential names, and the small text helpers shared by the page.
import { assetUrl, ASSET_V } from "./assets.js";

export const HINTS_KEY = "aether_settings_hints";

// The rail and the content share one order. The first six entries are
// local surfaces; every `feed` entry renders the backend's matching group.
export const SECTION_META = [
  { key: "general", group: "Preferences", title: "General", icon: "sliders" },
  { key: "llm", group: "Preferences", title: "Model", icon: "layers", feed: true },
  { key: "agent", group: "Preferences", title: "Behavior", icon: "pulse", feed: true },
  { key: "salience", group: "Preferences", title: "Attention", icon: "eye", feed: true },
  { key: "authz", group: "Safety", title: "Approvals", icon: "shield", feed: true },
  { key: "proactive", group: "Safety", title: "Background work", icon: "moon", feed: true },
  { key: "messaging", group: "Connections", title: "Chat surfaces", icon: "chat", feed: true },
  { key: "mcp_servers", group: "Connections", title: "App servers", icon: "server", feed: true },
  { key: "credentials", group: "Connections", title: "Credentials", icon: "key" },
  { key: "profile", group: "You", title: "What Aether knows about you", icon: "user" },
  { key: "personality", group: "You", title: "Personality", icon: "pen" },
  { key: "access", group: "You", title: "Access", icon: "lock" },
  { key: "workbench", group: "System", title: "Cloud workbench", icon: "cloud", feed: true },
  { key: "media", group: "System", title: "Media", icon: "image", feed: true },
];

export const GROUP_ORDER = ["Preferences", "Safety", "Connections", "You", "System"];

export const CHOICE_LABELS = {
  modal: "Modal",
  vercel: "Vercel",
  pollinations: "Pollinations",
  openai: "OpenAI",
  gemini: "Gemini",
  anthropic: "Anthropic",
  local: "Local",
  off: "None (off)",
  default: "Use default",
};

const SERVICE_FACES = {
  telegram: { imgSrc: assetUrl(`icon-telegram.png?v=${ASSET_V}`), label: "Telegram" },
  discord: { imgSrc: assetUrl(`icon-discord.png?v=${ASSET_V}`), label: "Discord" },
  mail: { imgSrc: assetUrl(`icon-email.jpg?v=${ASSET_V}`), label: "Mail" },
  email: { imgSrc: assetUrl(`icon-email.jpg?v=${ASSET_V}`), label: "Email" },
  gmail: { imgSrc: assetUrl(`icon-gmail.png?v=${ASSET_V}`), label: "Gmail" },
  calendar: { imgSrc: assetUrl(`icon-calendar.png?v=${ASSET_V}`), label: "Calendar" },
  github: { imgSrc: assetUrl(`icon-github.png?v=${ASSET_V}`), label: "GitHub" },
  drive: { imgSrc: assetUrl(`icon-drive.png?v=${ASSET_V}`), label: "Google Drive" },
  docs: { imgSrc: assetUrl(`icon-docs.png?v=${ASSET_V}`), label: "Google Docs" },
  linkedin: { imgSrc: assetUrl(`icon-linkedin.png?v=${ASSET_V}`), label: "LinkedIn" },
  notion: { icon: "apps", label: "Notion" },
};

// Credentials arrive named by their environment key. The label underneath
// is the backend's plain phrase, which repeats for every provider key; the
// service name in front is the part a person actually needs.
const CREDENTIAL_TITLES = {
  ANTHROPIC_API_KEY: "Anthropic API key",
  OPENAI_API_KEY: "OpenAI API key",
  GEMINI_API_KEY: "Gemini API key",
  GROQ_API_KEY: "Groq voice key",
  NVIDIA_API_KEY: "NVIDIA API key",
  TELEGRAM_BOT_TOKEN: "Telegram bot token",
  DISCORD_BOT_TOKEN: "Discord bot token",
  TAVILY_API_KEY: "Tavily web search key",
  MODAL_TOKEN_ID: "Modal sandbox key",
  MODAL_TOKEN_SECRET: "Modal sandbox secret",
  VERCEL_TOKEN: "Vercel backup token",
  VERCEL_TEAM_ID: "Vercel team id",
  VERCEL_PROJECT_ID: "Vercel project id",
  GITHUB_ACTIONS_TOKEN: "GitHub batch queue key",
};

export const readHints = () => {
  try {
    return localStorage.getItem(HINTS_KEY) !== "0";
  } catch {
    return true;
  }
};

export const normalise = (value) => String(value || "").toLowerCase();

export const credentialTitle = (cred) => CREDENTIAL_TITLES[cred.name] || cred.label;

// The stale pre-cutover composio entry is ignored at boot and on writes, so
// it must not read as a live app server here either.
const staleField = (groupKey, path) =>
  groupKey === "mcp_servers" && path === "mcp_servers.composio.enabled";

export const cleanGroup = (group) => ({
  ...group,
  fields: group.fields.filter((field) => !staleField(group.key, field.path)),
});

export const humanise = (value) =>
  String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b[a-z]/g, (letter) => letter.toUpperCase());

export const faceFor = (path) => {
  const key = String(path).split(".")[1]?.toLowerCase() || "";
  return SERVICE_FACES[key] || { icon: "server", label: humanise(key) };
};
