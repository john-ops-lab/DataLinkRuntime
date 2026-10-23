import { ApiError } from "./api";
import { currentSystemLocale, i18n } from "./i18n";
import { userErrorMessage } from "./user-message";

/** Localized stable source errors; upstream messages never become UI detail. */
export function subscriptionErrorMessage(
  error: unknown,
  sourceType?: string,
): string {
  const locale = currentSystemLocale();
  const generic = error instanceof ApiError ? `errors.${error.code}` : "";
  const specific =
    error instanceof ApiError && sourceType === "kubernetes"
      ? `kubernetes.errors.${error.code}`
      : "";
  const key =
    specific && i18n.exists(specific, { lng: locale, ns: "subscription" })
      ? specific
      : generic;
  const explanation =
    key && i18n.exists(key, { lng: locale, ns: "subscription" })
      ? i18n.getFixedT(locale, "subscription")(key)
      : undefined;
  return userErrorMessage(error, explanation, locale);
}
