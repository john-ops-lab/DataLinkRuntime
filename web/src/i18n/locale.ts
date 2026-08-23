import type { SystemLocale } from "../types";

export const DEFAULT_SYSTEM_LOCALE: SystemLocale = "zh-CN";
export const SYSTEM_LOCALES = ["zh-CN", "en"] as const satisfies readonly SystemLocale[];
export const SYSTEM_LOCALE_STORAGE_KEY = "dlr-system-locale";

export function isSystemLocale(value: unknown): value is SystemLocale {
  return value === "zh-CN" || value === "en";
}

export function readCachedSystemLocale(): SystemLocale {
  if (typeof window === "undefined") {
    return DEFAULT_SYSTEM_LOCALE;
  }
  const cached = window.localStorage.getItem(SYSTEM_LOCALE_STORAGE_KEY);
  return isSystemLocale(cached) ? cached : DEFAULT_SYSTEM_LOCALE;
}

export function cacheSystemLocale(locale: SystemLocale): void {
  window.localStorage.setItem(SYSTEM_LOCALE_STORAGE_KEY, locale);
}

export function resolveSystemLocale(value: string | undefined): SystemLocale {
  return isSystemLocale(value) ? value : DEFAULT_SYSTEM_LOCALE;
}

/** Keep the document language aligned with the existing system/UI locale source. */
export function syncDocumentLanguage(locale: SystemLocale): void {
  if (typeof document !== "undefined") {
    document.documentElement.lang = locale;
  }
}
