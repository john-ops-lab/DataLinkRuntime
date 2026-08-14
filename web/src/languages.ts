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
    "    try:\n" +
    "        return {\"message\": \"hello from DLR\", \"input\": input}\n" +
    "    finally:\n" +
    "        context.logger.info(\"任务结束\")\n",
  javascript:
    "export async function handle(context, input) {\n" +
    "  context.logger.info(\"任务开始\");\n" +
    "  try {\n" +
    "    return { message: \"hello from DLR\", input };\n" +
    "  } finally {\n" +
    "    context.logger.info(\"任务结束\");\n" +
    "  }\n" +
    "}\n",
  java:
    "public class Adapter {\n" +
    "    public Object handle(Context context, Object input) throws Exception {\n" +
    "        context.logger.info(\"任务开始\");\n" +
    "        try {\n" +
    "            return input;\n" +
    "        } finally {\n" +
    "            context.logger.info(\"任务结束\");\n" +
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
