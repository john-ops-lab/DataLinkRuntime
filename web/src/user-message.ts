import { ApiError } from "./api";
import type { SystemLocale } from "./types";

const HAS_CHINESE = /[\u3400-\u9fff]/;

/**
 * Keep the user-facing primary error in Chinese while retaining the stable
 * domain code as secondary troubleshooting information. Raw upstream English
 * messages are deliberately not promoted into the Console UI.
 */
export function userErrorMessage(
  error: unknown,
  fallback = "请求失败",
  locale: SystemLocale = "zh-CN",
): string {
  if (!(error instanceof ApiError)) {
    return fallback;
  }

  const primary =
    locale === "en"
      ? HAS_CHINESE.test(error.message)
        ? fallback
        : error.message
      : HAS_CHINESE.test(error.message)
        ? error.message
        : fallback;
  return locale === "en"
    ? `${primary} (Error code: ${error.code})`
    : `${primary}（错误码：${error.code}）`;
}
