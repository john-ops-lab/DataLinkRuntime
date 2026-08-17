import type { AdapterLanguage } from "./types";

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
  python: { label: "Python 依赖", placeholder: "如 requests==2.32.3（每行一个依赖）" },
  javascript: { label: "npm 依赖", placeholder: "如 axios@1.7.7（每行一个依赖）" },
  java: {
    label: "Maven 依赖",
    placeholder: "如 com.squareup.okhttp3:okhttp:4.12.0（每行一个依赖）",
  },
};
