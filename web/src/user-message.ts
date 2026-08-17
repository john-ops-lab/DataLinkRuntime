import { ApiError } from "./api";
import { currentSystemLocale, i18n } from "./i18n";
import type { SystemLocale } from "./types";

const HAS_CHINESE = /[\u3400-\u9fff]/;

/** Resolve a known domain code through the bundled locale resources. */
function translatedDomainError(error: ApiError, locale: SystemLocale): string | null {
  const key = `errors.${error.code}`;
  if (!i18n.exists(key, { lng: locale, ns: "common" })) {
    return null;
  }
  return i18n.getFixedT(locale, "common")(key, {
    ...error.params,
    defaultValue: "",
  });
}

/**
 * Use stable ``code + params`` for known domain errors. The server's message
 * remains a compatibility fallback only; unknown/provider text is never
 * promoted into the Console as the primary user message.
 */
export function userErrorMessage(
  error: unknown,
  fallback?: string,
  locale: SystemLocale = currentSystemLocale(),
): string {
  if (!(error instanceof ApiError)) {
    return fallback ?? i18n.getFixedT(locale, "common")("errors.unknown");
  }

  // Keep a server-provided Chinese compatibility message when it is already
  // localized. English/provider text still uses the caller fallback or the
  // bundled code translation, never the raw upstream message.
  const primary =
    locale === "zh-CN" && HAS_CHINESE.test(error.message)
      ? error.message
      : fallback ??
        translatedDomainError(error, locale) ??
        i18n.getFixedT(locale, "common")("errors.unknown");
  return locale === "en"
    ? `${primary} (Error code: ${error.code})`
    : `${primary}（错误码：${error.code}）`;
}
