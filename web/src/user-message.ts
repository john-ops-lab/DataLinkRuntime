import { ApiError } from "./api";

const HAS_CHINESE = /[\u3400-\u9fff]/;

/**
 * Keep the user-facing primary error in Chinese while retaining the stable
 * domain code as secondary troubleshooting information. Raw upstream English
 * messages are deliberately not promoted into the Console UI.
 */
export function userErrorMessage(error: unknown, fallback = "请求失败"): string {
  if (!(error instanceof ApiError)) {
    return fallback;
  }

  const primary = HAS_CHINESE.test(error.message) ? error.message : fallback;
  return `${primary}（错误码：${error.code}）`;
}
