import { currentSystemLocale, i18n } from "./i18n";
import type { AdapterLanguage, AdapterType, SystemLocale } from "./types";

export const LANGUAGE_LABELS: Record<AdapterLanguage, string> = {
  python: "Python",
  javascript: "JavaScript",
  java: "Java",
};

export const STARTER_CODE: Record<AdapterLanguage, string> = {
  python: "def handle(context, input):\n    return input\n",
  javascript: "export async function handle(context, input) {\n  return input;\n}\n",
  java:
    "public class Adapter {\n" +
    "    public Object handle(Context context, Object input) throws Exception {\n" +
    "        return input;\n" +
    "    }\n" +
    "}\n",
};

export const TASK_STARTER_CODE: Record<AdapterLanguage, string> = {
  python:
    "def handle(context, input):\n" +
    "    context.logger.info(\"任务开始\")\n" +
    "    # 读取“凭据绑定”中配置的密码，不要把真实密码直接写进代码\n" +
    "    password = context.secrets.get(\"PASSWORD\")\n" +
    "    try:\n" +
    "        # 在这里使用 password 调用目标系统，但不要打印 password\n" +
    "        return {\"message\": \"hello from DLR\", \"input\": input}\n" +
    "    finally:\n" +
    "        context.logger.info(\"任务结束\")\n",
  javascript:
    "export async function handle(context, input) {\n" +
    "  context.logger.info(\"任务开始\");\n" +
    "  // 读取“凭据绑定”中配置的密码，不要把真实密码直接写进代码\n" +
    "  const password = context.secrets.get(\"PASSWORD\");\n" +
    "  try {\n" +
    "    // 在这里使用 password 调用目标系统，但不要打印 password\n" +
    "    return { message: \"hello from DLR\", input };\n" +
    "  } finally {\n" +
    "    context.logger.info(\"任务结束\");\n" +
    "  }\n" +
    "}\n",
  java:
    "public class Adapter {\n" +
    "    public Object handle(Context context, Object input) throws Exception {\n" +
    "        context.logger.info(\"任务开始\");\n" +
    "        // 读取“凭据绑定”中配置的密码，不要把真实密码直接写进代码\n" +
    "        String password = context.secrets.get(\"PASSWORD\");\n" +
    "        try {\n" +
    "            // 在这里使用 password 调用目标系统，但不要打印 password\n" +
    "            return input;\n" +
    "        } finally {\n" +
    "            context.logger.info(\"任务结束\");\n" +
    "        }\n" +
    "    }\n" +
    "}\n",
};

export const WEBHOOK_STARTER_CODE: Record<AdapterLanguage, string> = {
  python:
    "def handle(context, input):\n" +
    "    context.logger.info(\"收到 Webhook 请求\")\n" +
    "    # 读取“凭据绑定”中配置的令牌，不要把真实 Token 直接写进代码\n" +
    "    token = context.secrets.get(\"TOKEN\")\n" +
    "    try:\n" +
    "        # 在这里使用 token 校验或调用目标系统，但不要打印 token\n" +
    "        return {\"received\": True, \"data\": input}\n" +
    "    finally:\n" +
    "        context.logger.info(\"处理完 Webhook 请求\")\n",
  javascript:
    "export async function handle(context, input) {\n" +
    "  context.logger.info(\"收到 Webhook 请求\");\n" +
    "  // 读取“凭据绑定”中配置的令牌，不要把真实 Token 直接写进代码\n" +
    "  const token = context.secrets.get(\"TOKEN\");\n" +
    "  try {\n" +
    "    // 在这里使用 token 校验或调用目标系统，但不要打印 token\n" +
    "    return { received: true, data: input };\n" +
    "  } finally {\n" +
    "    context.logger.info(\"处理完 Webhook 请求\");\n" +
    "  }\n" +
    "}\n",
  java:
    "public class Adapter {\n" +
    "    public Object handle(Context context, Object input) throws Exception {\n" +
    "        context.logger.info(\"收到 Webhook 请求\");\n" +
    "        // 读取“凭据绑定”中配置的令牌，不要把真实 Token 直接写进代码\n" +
    "        String token = context.secrets.get(\"TOKEN\");\n" +
    "        try {\n" +
    "            // 在这里使用 token 校验或调用目标系统，但不要打印 token\n" +
    "            return input;\n" +
    "        } finally {\n" +
    "            context.logger.info(\"处理完 Webhook 请求\");\n" +
    "        }\n" +
    "    }\n" +
    "}\n",
};

const zhRuntimeT = i18n.getFixedT("zh-CN", "runtime");

/** zh-CN compatibility exports retained for the existing starter contract tests. */
export const DEPENDENCY_UI: Record<
  AdapterLanguage,
  { label: string; placeholder: string }
> = {
  python: {
    label: zhRuntimeT("dependencies.python.label"),
    placeholder: zhRuntimeT("dependencies.python.placeholder"),
  },
  javascript: {
    label: zhRuntimeT("dependencies.javascript.label"),
    placeholder: zhRuntimeT("dependencies.javascript.placeholder"),
  },
  java: {
    label: zhRuntimeT("dependencies.java.label"),
    placeholder: zhRuntimeT("dependencies.java.placeholder"),
  },
};

/** zh-CN compatibility export retained for the existing dependency contract tests. */
export const DEPENDENCY_NOTE = zhRuntimeT("dependencies.note");

const EN_TASK_STARTER_CODE: Record<AdapterLanguage, string> = {
  python:
    "def handle(context, input):\n" +
    "    context.logger.info(\"Task started\")\n" +
    "    # Read the password configured in Credential bindings; never put the real password in code\n" +
    "    password = context.secrets.get(\"PASSWORD\")\n" +
    "    try:\n" +
    "        # Use password to call the target system here, but never print it\n" +
    "        return {\"message\": \"hello from DLR\", \"input\": input}\n" +
    "    finally:\n" +
    "        context.logger.info(\"Task finished\")\n",
  javascript:
    "export async function handle(context, input) {\n" +
    "  context.logger.info(\"Task started\");\n" +
    "  // Read the password configured in Credential bindings; never put the real password in code\n" +
    "  const password = context.secrets.get(\"PASSWORD\");\n" +
    "  try {\n" +
    "    // Use password to call the target system here, but never print it\n" +
    "    return { message: \"hello from DLR\", input };\n" +
    "  } finally {\n" +
    "    context.logger.info(\"Task finished\");\n" +
    "  }\n" +
    "}\n",
  java:
    "public class Adapter {\n" +
    "    public Object handle(Context context, Object input) throws Exception {\n" +
    "        context.logger.info(\"Task started\");\n" +
    "        // Read the password configured in Credential bindings; never put the real password in code\n" +
    "        String password = context.secrets.get(\"PASSWORD\");\n" +
    "        try {\n" +
    "            // Use password to call the target system here, but never print it\n" +
    "            return input;\n" +
    "        } finally {\n" +
    "            context.logger.info(\"Task finished\");\n" +
    "        }\n" +
    "    }\n" +
    "}\n",
};

const EN_WEBHOOK_STARTER_CODE: Record<AdapterLanguage, string> = {
  python:
    "def handle(context, input):\n" +
    "    context.logger.info(\"Webhook request received\")\n" +
    "    # Read the token configured in Credential bindings; never put the real Token in code\n" +
    "    token = context.secrets.get(\"TOKEN\")\n" +
    "    try:\n" +
    "        # Use token to validate or call the target system here, but never print it\n" +
    "        return {\"received\": True, \"data\": input}\n" +
    "    finally:\n" +
    "        context.logger.info(\"Webhook request processed\")\n",
  javascript:
    "export async function handle(context, input) {\n" +
    "  context.logger.info(\"Webhook request received\");\n" +
    "  // Read the token configured in Credential bindings; never put the real Token in code\n" +
    "  const token = context.secrets.get(\"TOKEN\");\n" +
    "  try {\n" +
    "    // Use token to validate or call the target system here, but never print it\n" +
    "    return { received: true, data: input };\n" +
    "  } finally {\n" +
    "    context.logger.info(\"Webhook request processed\");\n" +
    "  }\n" +
    "}\n",
  java:
    "public class Adapter {\n" +
    "    public Object handle(Context context, Object input) throws Exception {\n" +
    "        context.logger.info(\"Webhook request received\");\n" +
    "        // Read the token configured in Credential bindings; never put the real Token in code\n" +
    "        String token = context.secrets.get(\"TOKEN\");\n" +
    "        try {\n" +
    "            // Use token to validate or call the target system here, but never print it\n" +
    "            return input;\n" +
    "        } finally {\n" +
    "            context.logger.info(\"Webhook request processed\");\n" +
    "        }\n" +
    "    }\n" +
    "}\n",
};

/** Stable locale-specific starter snapshots used only for a new unsaved Adapter. */
export function starterCodeFor(
  language: AdapterLanguage,
  adapterType: AdapterType,
  locale: SystemLocale = currentSystemLocale(),
): string {
  if (locale === "en") {
    return adapterType === "task" ? EN_TASK_STARTER_CODE[language] : EN_WEBHOOK_STARTER_CODE[language];
  }
  return adapterType === "task" ? TASK_STARTER_CODE[language] : WEBHOOK_STARTER_CODE[language];
}

export function dependencyUiFor(
  language: AdapterLanguage,
  locale: SystemLocale = currentSystemLocale(),
): { label: string; placeholder: string } {
  const t = i18n.getFixedT(locale, "runtime");
  return {
    label: t(`dependencies.${language}.label`),
    placeholder: t(`dependencies.${language}.placeholder`),
  };
}

export function dependencyNoteFor(locale: SystemLocale = currentSystemLocale()): string {
  return i18n.getFixedT(locale, "runtime")("dependencies.note");
}
