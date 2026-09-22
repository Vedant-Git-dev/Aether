import type {
  SessionsCatalogStartTerminalParams,
  SessionsCatalogStartTerminalResult,
  UiCommandParams,
} from "@aether/gateway-protocol";
import {
  KEYBOARD_SHORTCUT_COMBOS,
  matchesShortcutCombo,
} from "../lib/keyboard-shortcut-contract.ts";
import type { BrowserTabTarget } from "./browser/browser-target.ts";

export const TERMINAL_PANEL_TOGGLE_EVENT = "aether:terminal-toggle";
export const TERMINAL_PANEL_DOCK_BOTTOM_EVENT = "aether:terminal-dock-bottom";
export const BROWSER_PANEL_TOGGLE_EVENT = "aether:browser-toggle";
export const DESKTOP_PANEL_TOGGLE_EVENT = "aether:desktop-toggle";
export const HOME_PANEL_TOGGLE_EVENT = "aether:home-toggle";
export const CUSTODIAN_PANEL_TOGGLE_EVENT = "aether:custodian-toggle";
export const DEBUG_OVERLAY_REQUEST_EVENT = "aether:debug-overlay-request";
export const KEYBOARD_SHORTCUTS_REQUEST_EVENT = "aether:keyboard-shortcuts-request";
export const UI_COMMAND_EVENT = "aether:ui-command";

export type UiCommandDetail = UiCommandParams;

export type TerminalPanelToggleDetail = {
  agentId?: string | null;
  dock?: "bottom" | "right";
  open?: boolean;
  terminalSessionId?: string;
  agentOwned?: boolean;
  catalog?: {
    catalogId: string;
    hostId: string;
    threadId: string;
  };
  catalogStart?: {
    params: SessionsCatalogStartTerminalParams;
    isCurrent: () => boolean;
    respondWith: (result: Promise<SessionsCatalogStartTerminalResult>) => void;
  };
};

export type BrowserPanelToggleDetail = {
  dock?: "bottom" | "right";
  newTab?: boolean;
  open?: boolean;
  /** Existing tab to focus when the panel opens (browser-tab chat cards). */
  browserTab?: BrowserTabTarget;
  url?: string;
  /** User-opened WKWebView tab on the native macOS host. */
  native?: boolean;
};

export type DesktopPanelToggleDetail = {
  dock?: "bottom" | "right";
  open?: boolean;
  environmentId?: string;
};

export type PanelToggleElement = HTMLElement & {
  handleToggleRequest: (event: Event) => void;
};

export function isTerminalPanelShortcut(event: KeyboardEvent): boolean {
  return matchesShortcutCombo(KEYBOARD_SHORTCUT_COMBOS.terminalPanel, event);
}

export function isHomePanelShortcut(event: KeyboardEvent): boolean {
  return matchesShortcutCombo(KEYBOARD_SHORTCUT_COMBOS.homePanel, event);
}
