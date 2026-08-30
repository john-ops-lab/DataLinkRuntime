import { useEffect, useState } from "react";

import { applyUiLocale, currentSystemLocale, DEFAULT_SYSTEM_LOCALE, i18n, isSystemLocale } from "./i18n";
import type { SystemLocale } from "./types";

/** Separate from the deployment-locale cache: this is only the current browser's login preference. */
export const LOGIN_LOCALE_STORAGE_KEY = "dlr-login-locale";

export function readLoginLocalePreference(): SystemLocale | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const value = window.localStorage.getItem(LOGIN_LOCALE_STORAGE_KEY);
    return isSystemLocale(value) ? value : null;
  } catch {
    return null;
  }
}

export function cacheLoginLocalePreference(locale: SystemLocale): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(LOGIN_LOCALE_STORAGE_KEY, locale);
  } catch {
    // Storage can be unavailable in private or policy-restricted contexts;
    // choosing a login language must never block authentication.
  }
}

export function preferredLoginLocale(_serverLocale?: SystemLocale): SystemLocale {
  // The public deployment locale must not silently choose the login language.
  // Until the user explicitly picks one here, the login surface is zh-CN.
  // Keep the optional argument for callers compiled against the previous
  // helper signature; server locale is intentionally ignored for login.
  void _serverLocale;
  return readLoginLocalePreference() ?? DEFAULT_SYSTEM_LOCALE;
}

/** Apply the browser preference without changing the deployment system-locale cache. */
export async function applyLoginLocalePreference(serverLocale?: SystemLocale): Promise<SystemLocale> {
  const locale = preferredLoginLocale(serverLocale);
  await applyUiLocale(locale);
  return locale;
}

export function useLoginLocale(loginSurface = true): [SystemLocale, (locale: SystemLocale) => void] {
  const [explicitLocale, setExplicitLocale] = useState<SystemLocale | null>(null);

  useEffect(() => {
    if (!loginSurface) {
      return;
    }
    const preferred = preferredLoginLocale();
    if ((i18n.resolvedLanguage ?? i18n.language) !== preferred) {
      void applyUiLocale(preferred);
    }
  }, [loginSurface]);

  function selectLocale(nextLocale: SystemLocale): void {
    cacheLoginLocalePreference(nextLocale);
    setExplicitLocale(nextLocale);
    void applyUiLocale(nextLocale);
  }

  if (!loginSurface) {
    return [currentSystemLocale(), () => undefined];
  }
  return [explicitLocale ?? preferredLoginLocale(), selectLocale];
}
