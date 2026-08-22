import { useEffect, useState } from "react";

import { applyUiLocale, currentSystemLocale, i18n, isSystemLocale } from "./i18n";
import type { SystemLocale } from "./types";

/** Separate from the deployment-locale cache: this is only the current browser's login preference. */
export const LOGIN_LOCALE_STORAGE_KEY = "dlr-login-locale";

export function readLoginLocalePreference(): SystemLocale | null {
  if (typeof window === "undefined") {
    return null;
  }
  const value = window.localStorage.getItem(LOGIN_LOCALE_STORAGE_KEY);
  return isSystemLocale(value) ? value : null;
}

export function cacheLoginLocalePreference(locale: SystemLocale): void {
  window.localStorage.setItem(LOGIN_LOCALE_STORAGE_KEY, locale);
}

export function preferredLoginLocale(serverLocale?: SystemLocale): SystemLocale {
  return readLoginLocalePreference() ?? serverLocale ?? currentSystemLocale();
}

/** Apply the browser preference without changing the deployment system-locale cache. */
export async function applyLoginLocalePreference(serverLocale?: SystemLocale): Promise<SystemLocale> {
  const locale = preferredLoginLocale(serverLocale);
  await applyUiLocale(locale);
  return locale;
}

export function useLoginLocale(): [SystemLocale, (locale: SystemLocale) => void] {
  const [explicitLocale, setExplicitLocale] = useState<SystemLocale | null>(null);

  useEffect(() => {
    const preferred = readLoginLocalePreference();
    if (preferred !== null && (i18n.resolvedLanguage ?? i18n.language) !== preferred) {
      void applyUiLocale(preferred);
    }
  }, []);

  function selectLocale(nextLocale: SystemLocale): void {
    cacheLoginLocalePreference(nextLocale);
    setExplicitLocale(nextLocale);
    void applyUiLocale(nextLocale);
  }

  return [explicitLocale ?? readLoginLocalePreference() ?? currentSystemLocale(), selectLocale];
}
