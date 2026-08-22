import { afterEach, describe, expect, it } from "vitest";

import { ApiError } from "./api";
import { credentialFieldLabel, credentialTypeLabel } from "./credential-fields";
import {
  applySystemLocale,
  DEFAULT_SYSTEM_LOCALE,
} from "./i18n";
import { dependencyNoteFor, dependencyUiFor, starterCodeFor } from "./languages";
import { packageSourcePresetLabel } from "./package-source-catalog";
import { userErrorMessage } from "./user-message";

const LANGUAGES = ["python", "javascript", "java"] as const;

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
});

describe("Wave 2 C error and starter contracts", () => {
  it("localizes a known error from stable params and safely falls back for unknown codes", async () => {
    const known = new ApiError(
      413,
      "execution_input_too_large",
      "raw third-party message",
      { max_bytes: 128 },
    );
    expect(userErrorMessage(known, undefined, "en")).toBe(
      "Input exceeds the 128 byte limit (Error code: execution_input_too_large)",
    );

    const unknown = new ApiError(502, "provider_raw_error", "third-party raw output");
    const fallback = userErrorMessage(unknown, undefined, "en");
    expect(fallback).toBe("Request failed (Error code: provider_raw_error)");
    expect(fallback).not.toContain("third-party raw output");
  });

  it.each(LANGUAGES)("provides both Task and Webhook %s starter snapshots", (language) => {
    const zhTask = starterCodeFor(language, "task", "zh-CN");
    const enTask = starterCodeFor(language, "task", "en");
    const zhWebhook = starterCodeFor(language, "webhook", "zh-CN");
    const enWebhook = starterCodeFor(language, "webhook", "en");

    for (const code of [zhTask, enTask, zhWebhook, enWebhook]) {
      expect(code).toContain("handle");
      expect(code).toContain("input");
      expect(code).not.toContain('context.secrets.get("TOKEN")');
      expect(code).not.toMatch(/password\s*=\s*["']/i);
      expect(code).not.toMatch(/token\s*=\s*["']/i);
    }
    expect(zhTask).toContain("任务开始");
    expect(zhWebhook).toContain("收到 Webhook 请求");
    expect(enTask).toContain("Task started");
    expect(enWebhook).toContain("Webhook request received");
    expect(enTask).not.toContain("任务开始");
    expect(enWebhook).not.toContain("收到 Webhook 请求");
    expect(zhWebhook).toContain("Bearer");
    expect(enWebhook).toContain("Bearer");
  });

  it("does not mutate a captured starter snapshot when the system locale changes", async () => {
    const existingCode = starterCodeFor("python", "task", "zh-CN");
    await applySystemLocale("en");
    expect(existingCode).toContain("任务开始");
    expect(starterCodeFor("python", "task", "en")).toContain("Task started");
  });

  it("localizes dependency labels, placeholders and notes for English consumers", () => {
    expect(dependencyUiFor("python", "en")).toEqual({
      label: "Python dependencies",
      placeholder: "e.g. requests==2.32.3 (one dependency per line)",
    });
    expect(dependencyNoteFor("en")).toContain("The Worker installs these dependencies");
    expect(dependencyNoteFor("en")).not.toContain("Worker 执行前会安装这些依赖");
  });

  it("keeps stable Credential IDs and dependency preset IDs separate from display text", async () => {
    expect(credentialTypeLabel("access_key", "zh-CN")).toBe("访问密钥");
    expect(credentialTypeLabel("access_key", "en")).toBe("Access Key");
    expect(credentialFieldLabel("access_key_secret", "en")).toBe("Access Key Secret");

    const preset = { name: "阿里云 PyPI 镜像", preset_id: "pypi.aliyun" } as const;
    expect(preset.preset_id).toBe("pypi.aliyun");
    expect(packageSourcePresetLabel(preset, "zh-CN")).toBe("阿里云 PyPI 镜像");
    expect(packageSourcePresetLabel(preset, "en")).toBe("Aliyun PyPI mirror");

    const userSource = { name: "我的私有源", preset_id: null } as const;
    expect(packageSourcePresetLabel(userSource, "en")).toBe("我的私有源");
  });
});
