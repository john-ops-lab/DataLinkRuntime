import type { SystemLocale } from "../types";
import { userErrorMessage } from "../user-message";

/** Keep known login notices translatable after the login language changes. */
export type LoginNotice = string | { key: string } | { error: unknown; fallbackKey: string };

export function loginNoticeText(
  notice: LoginNotice | null,
  locale: SystemLocale,
  translate: (key: string) => string,
): string | null {
  if (notice === null || typeof notice === "string") return notice;
  return "key" in notice
    ? translate(notice.key)
    : userErrorMessage(notice.error, translate(notice.fallbackKey), locale);
}
