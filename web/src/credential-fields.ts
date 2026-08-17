/** 凭据类型字段约定（镜像后端 models/platform.py 的 CREDENTIAL_FIELDS）。 */

export const CREDENTIAL_TYPE_FIELDS: Record<string, readonly string[]> = {
  password: ["username", "password"],
  token: ["token"],
  // M5.5.7：访问密钥字段统一为 access_key_id + access_key_secret。
  access_key: ["access_key_id", "access_key_secret"],
  secret: ["value"],
};

export const CREDENTIAL_TYPE_LABELS: Record<string, string> = {
  password: "密码",
  token: "令牌",
  access_key: "访问密钥",
  secret: "通用密钥",
};

/** 该凭据类型可绑定到的字段列表；未知类型返回空列表。 */
export function credentialFields(type: string): readonly string[] {
  return CREDENTIAL_TYPE_FIELDS[type] ?? [];
}
