import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import aiEn from "./locales/en/ai.json";
import commonEn from "./locales/en/common.json";
import adapterEn from "./locales/en/adapter.json";
import runtimeEn from "./locales/en/runtime.json";
import settingsEn from "./locales/en/settings.json";
import aiZhCN from "./locales/zh-CN/ai.json";
import commonZhCN from "./locales/zh-CN/common.json";
import adapterZhCN from "./locales/zh-CN/adapter.json";
import runtimeZhCN from "./locales/zh-CN/runtime.json";
import settingsZhCN from "./locales/zh-CN/settings.json";
import {
  cacheSystemLocale,
  DEFAULT_SYSTEM_LOCALE,
  readCachedSystemLocale,
  resolveSystemLocale,
} from "./locale";
import type { SystemLocale } from "../types";

export const resources = {
  "zh-CN": {
    common: commonZhCN,
    adapter: adapterZhCN,
    runtime: runtimeZhCN,
    settings: settingsZhCN,
    ai: aiZhCN,
  },
  en: {
    common: commonEn,
    adapter: adapterEn,
    runtime: runtimeEn,
    settings: settingsEn,
    ai: aiEn,
  },
} as const;

void i18n.use(initReactI18next).init({
  lng: readCachedSystemLocale(),
  fallbackLng: DEFAULT_SYSTEM_LOCALE,
  supportedLngs: ["zh-CN", "en"],
  ns: ["common", "adapter", "runtime", "settings", "ai"],
  defaultNS: "common",
  resources,
  interpolation: { escapeValue: false },
  returnNull: false,
  returnEmptyString: false,
  parseMissingKeyHandler: (key, defaultValue, options) => {
    if (typeof defaultValue === "string" && defaultValue !== key) {
      return defaultValue;
    }
    const locale = resolveSystemLocale(
      typeof options?.lng === "string" ? options.lng : i18n.language,
    );
    return resources[locale].common.translation.unavailable;
  },
});

export async function applySystemLocale(locale: SystemLocale): Promise<void> {
  cacheSystemLocale(locale);
  await i18n.changeLanguage(locale);
}

/** Change the current browser UI locale without changing the deployment default cache. */
export async function applyUiLocale(locale: SystemLocale): Promise<void> {
  await i18n.changeLanguage(locale);
}

export function currentSystemLocale(): SystemLocale {
  return resolveSystemLocale(i18n.resolvedLanguage ?? i18n.language);
}

export { i18n };
export * from "./locale";
export default i18n;
