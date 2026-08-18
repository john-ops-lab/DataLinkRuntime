/** 凭据类型字段约定（镜像后端 models/platform.py 的 CREDENTIAL_FIELDS）。 */

import { currentSystemLocale, i18n } from "./i18n";
import type { SystemLocale } from "./types";

export const CREDENTIAL_TYPE_FIELDS: Record<string, readonly string[]> = {
  password: ["username", "password"],
  token: ["token"],
  // M5.5.7：访问密钥字段统一为 access_key_id + access_key_secret。
  access_key: ["access_key_id", "access_key_secret"],
  secret: ["value"],
};

export const CREDENTIAL_TYPE_LABELS: Record<string, string> = {
  password: "password",
  token: "token",
  access_key: "access_key",
  secret: "secret",
};

const CREDENTIAL_TYPE_LABEL_KEYS: Record<string, string> = {
  password: "credentialTypes.password",
  token: "credentialTypes.token",
  access_key: "credentialTypes.access_key",
  secret: "credentialTypes.secret",
};

const CREDENTIAL_FIELD_LABEL_KEYS: Record<string, string> = {
  username: "credentialFields.username",
  password: "credentialFields.password",
  token: "credentialFields.token",
  access_key_id: "credentialFields.access_key_id",
  access_key_secret: "credentialFields.access_key_secret",
  value: "credentialFields.value",
};

/** 该凭据类型可绑定到的字段列表；未知类型返回空列表。 */
export function credentialFields(type: string): readonly string[] {
  return CREDENTIAL_TYPE_FIELDS[type] ?? [];
}

/** Translate a stable Credential type ID for display only. */
export function credentialTypeLabel(
  type: string,
  locale: SystemLocale = currentSystemLocale(),
): string {
  const key = CREDENTIAL_TYPE_LABEL_KEYS[type];
  if (key === undefined) {
    return type;
  }
  return i18n.getFixedT(locale, "settings")(key, {
    defaultValue: CREDENTIAL_TYPE_LABELS[type] ?? type,
  });
}

/** Translate a stable Credential field ID for display only. */
export function credentialFieldLabel(
  field: string,
  locale: SystemLocale = currentSystemLocale(),
): string {
  const key = CREDENTIAL_FIELD_LABEL_KEYS[field];
  if (key === undefined) {
    return field;
  }
  return i18n.getFixedT(locale, "settings")(key, { defaultValue: field });
}
