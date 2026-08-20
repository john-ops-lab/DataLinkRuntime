/** The Web image serves this file from Nginx per public entry port. */

declare global {
  interface Window {
    __DLR_ENTRY_MODE__?: "token" | "account";
  }
}

export type EntryMode = "token" | "account";

export function currentEntryMode(): EntryMode {
  return window.__DLR_ENTRY_MODE__ === "account" ? "account" : "token";
}
