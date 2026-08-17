import { currentSystemLocale } from "./i18n";
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

export const DEPENDENCY_UI: Record<
  AdapterLanguage,
  { label: string; placeholder: string }
> = {
  python: { label: "Python 依赖", placeholder: "如 requests==2.32.3（回车换行，每行写一个依赖）" },
  javascript: {
    label: "JavaScript 依赖",
    placeholder: "如 axios@1.7.7（回车换行，每行写一个依赖）",
  },
  java: {
    label: "Java 依赖",
    placeholder: "如 org.apache.commons:commons-lang3:3.17.0（回车换行，每行写一个依赖）",
  },
};

/** 三种语言共用的依赖安装说明（M5.5.8）。 */
export const DEPENDENCY_NOTE =
  "Worker 执行前会安装这些依赖。安装源需在“系统设置”中配置；如为离线/企业网络环境，可由管理员预置对应依赖源或缓存。不填写则平台不会额外检查依赖是否齐全，缺少依赖可能在运行时直接报错。";

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
  if (locale === "en") {
    return {
      python: { label: "Python dependencies", placeholder: "e.g. requests==2.32.3 (one dependency per line)" },
      javascript: { label: "JavaScript dependencies", placeholder: "e.g. axios@1.7.7 (one dependency per line)" },
      java: { label: "Java dependencies", placeholder: "e.g. org.apache.commons:commons-lang3:3.17.0 (one dependency per line)" },
    }[language];
  }
  return DEPENDENCY_UI[language];
}

export function dependencyNoteFor(locale: SystemLocale = currentSystemLocale()): string {
  return locale === "en"
    ? "The Worker installs these dependencies before execution. Configure a source in System Settings; administrators can pre-populate a source or cache for offline and enterprise networks. An empty list skips the dependency check and missing packages may fail at runtime."
    : DEPENDENCY_NOTE;
}
