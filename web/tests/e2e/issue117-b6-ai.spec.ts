import { Buffer } from "node:buffer";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page, type Route } from "@playwright/test";

type Locale = "zh-CN" | "en";

const LOCALES: readonly Locale[] = ["zh-CN", "en"];
const VIEWPORTS = [1100, 1180, 1280, 1440, 1680, 1920] as const;
const specDir = dirname(fileURLToPath(import.meta.url));
const evidenceRoot = resolve(
  specDir,
  process.env.DLR_B6_OUTPUT_DIR ?? "../../../docs/evidence/issue117-b6/auxiliary-matrix",
);
const screenshotDir = resolve(evidenceRoot, "browser");

const adapter = {
  id: 1,
  name: "Batch 6 fixture adapter",
  description: "Deterministic browser fixture",
  language: "python",
  adapter_type: "task",
  run_mode: "manual",
  timeout_seconds: 300,
  owner_user_id: 42,
  latest_version_id: 10,
  runtime_worker_id: 1,
  runtime_locked: false,
  archived_at: null,
  running_execution_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  access_level: "admin",
};

const worker = {
  id: 1,
  name: "batch-6-fixture-worker",
  status: "online",
  last_heartbeat: "2026-01-01T00:00:00Z",
  capabilities: ["python"],
};

const version = {
  id: 10,
  adapter_id: 1,
  seq: 1,
  created_at: "2026-01-01T00:00:00Z",
};

const attachmentCapabilities = {
  limits: {
    max_attachments: 8,
    max_file_bytes: 6 * 1024 * 1024,
    max_total_bytes: 12 * 1024 * 1024,
    max_parsed_chars_per_file: 64 * 1024,
    max_parsed_total_chars: 256 * 1024,
    parse_timeout_seconds: 30,
  },
  supported_content_types: [
    "image/png",
    "image/jpeg",
    "image/webp",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
    "text/x-python",
  ],
};

interface GuidanceMetrics {
  line_count: number;
  line_height: number;
  height: number;
  white_space: string;
  hint_white_space: string;
  privacy_white_space: string;
  flex_wrap: string;
  scroll_width: number;
  client_width: number;
  hint_scroll_width: number;
  hint_client_width: number;
  privacy_scroll_width: number;
  privacy_client_width: number;
  text: string;
  hint_text: string;
  privacy_text: string;
  guidance_aria_label: string | null;
  hint_aria_label: string | null;
  privacy_aria_label: string | null;
}

interface CaseRecord {
  locale: Locale;
  width: number;
  screenshot: string;
  guidance: GuidanceMetrics;
  overflow: {
    inner_width: number;
    inner_height: number;
    document_scroll_width: number;
    body_scroll_width: number;
    document_scroll_height: number;
    body_scroll_height: number;
  };
  attachment_lifecycle: {
    picker_count: number;
    drag_count: number;
    invalid_count: number;
    after_remove_count: number;
    error_visible: boolean;
  };
  requests: {
    assist_count: number;
    assist_paths: string[];
    non_get_paths: string[];
    unknown_paths: string[];
  };
  console_errors: string[];
  page_errors: string[];
}

const records: CaseRecord[] = [];
let browserVersion = "unknown";

function jsonBody(body: unknown): string {
  return JSON.stringify(body);
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: jsonBody(body),
  });
}

async function installFixture(
  page: Page,
  locale: Locale,
): Promise<{
  unknownPaths: string[];
  assistPaths: string[];
  nonGetPaths: string[];
}> {
  const unknownPaths: string[] = [];
  const assistPaths: string[] = [];
  const nonGetPaths: string[] = [];

  await page.route("**/entry-mode.js", async (route) => {
    await route.fulfill({
      contentType: "application/javascript",
      body: 'window.__DLR_ENTRY_MODE__ = "token";',
    });
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname;
    if (method !== "GET") {
      nonGetPaths.push(method + " " + path);
    }

    if (path === "/api/locale" && method === "GET") {
      await fulfillJson(route, { locale });
      return;
    }
    if (path === "/api/health" && method === "GET") {
      await fulfillJson(route, { status: "ok", database: true });
      return;
    }
    if (path === "/api/workers" && method === "GET") {
      await fulfillJson(route, [worker]);
      return;
    }
    if (path === "/api/adapters" && method === "GET") {
      await fulfillJson(route, [adapter]);
      return;
    }
    if (path === "/api/adapters/1" && method === "GET") {
      await fulfillJson(route, adapter);
      return;
    }
    if (path === "/api/adapters/1/versions" && method === "GET") {
      await fulfillJson(route, [version]);
      return;
    }
    if (path === "/api/adapters/1/versions/10" && method === "GET") {
      await fulfillJson(route, {
        ...version,
        code: "def transform(payload):\n    return payload\n",
        requirements: "",
        runtime_config: {},
      });
      return;
    }
    if (path === "/api/adapters/1/credential-bindings" && method === "GET") {
      await fulfillJson(route, []);
      return;
    }
    if (path === "/api/adapters/1/credential-options" && method === "GET") {
      await fulfillJson(route, []);
      return;
    }
    if (path === "/api/ai/attachment-capabilities" && method === "GET") {
      await fulfillJson(route, attachmentCapabilities);
      return;
    }
    if (path === "/api/adapters/1/ai/knowledge-capability" && method === "GET") {
      await fulfillJson(route, { available: false, reason: null });
      return;
    }
    if (path === "/api/auth/admin/verify" && method === "GET") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/adapters/1/ai/assist" && method === "POST") {
      assistPaths.push(method + " " + path);
      await fulfillJson(route, {
        message: "Fixture response",
        provider: "fixture",
        model: "fixture-model",
        candidate: null,
        tool_calls: [],
      });
      return;
    }

    const requestKey = method + " " + path;
    unknownPaths.push(requestKey);
    await fulfillJson(route, { detail: { code: "fixture_unhandled", message: requestKey } }, 404);
  });

  return { unknownPaths, assistPaths, nonGetPaths };
}

async function readGuidance(page: Page): Promise<GuidanceMetrics> {
  return page.getByTestId("ai-composer-guidance").evaluate((element) => {
    const guidance = element as HTMLElement;
    const hint = guidance.querySelector<HTMLElement>("[data-testid='ai-attachment-hint']");
    const privacy = guidance.querySelector<HTMLElement>("[data-testid='ai-attachment-privacy']");
    const style = getComputedStyle(guidance);
    const hintStyle = hint === null ? null : getComputedStyle(hint);
    const privacyStyle = privacy === null ? null : getComputedStyle(privacy);
    const lineHeight = Number.parseFloat(style.lineHeight);
    return {
      line_count: Math.round(guidance.getBoundingClientRect().height / lineHeight),
      line_height: lineHeight,
      height: guidance.getBoundingClientRect().height,
      white_space: style.whiteSpace,
      hint_white_space: hintStyle?.whiteSpace ?? "missing",
      privacy_white_space: privacyStyle?.whiteSpace ?? "missing",
      flex_wrap: style.flexWrap,
      scroll_width: guidance.scrollWidth,
      client_width: guidance.clientWidth,
      hint_scroll_width: hint?.scrollWidth ?? 0,
      hint_client_width: hint?.clientWidth ?? 0,
      privacy_scroll_width: privacy?.scrollWidth ?? 0,
      privacy_client_width: privacy?.clientWidth ?? 0,
      text: guidance.textContent ?? "",
      hint_text: hint?.textContent ?? "",
      privacy_text: privacy?.textContent ?? "",
      guidance_aria_label: guidance.getAttribute("aria-label"),
      hint_aria_label: hint?.getAttribute("aria-label") ?? null,
      privacy_aria_label: privacy?.getAttribute("aria-label") ?? null,
    };
  });
}

async function readOverflow(page: Page) {
  return page.evaluate(() => ({
    inner_width: window.innerWidth,
    inner_height: window.innerHeight,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
    document_scroll_height: document.documentElement.scrollHeight,
    body_scroll_height: document.body.scrollHeight,
  }));
}

async function takeScreenshot(page: Page, name: string): Promise<string> {
  mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({ path: resolve(screenshotDir, name), fullPage: true });
  return "docs/evidence/issue117-b6/auxiliary-matrix/browser/" + name;
}

async function addDroppedFixtureFile(page: Page) {
  await page.getByTestId("ai-attachment-dropzone").evaluate((element) => {
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(new File(["drop fixture"], "drop.txt", { type: "text/plain" }));
    element.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer }));
  });
}

async function runCase(page: Page, locale: Locale, width: number) {
  const { unknownPaths, assistPaths, nonGetPaths } = await installFixture(page, locale);
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto("/");
  await expect(page.getByRole("heading", {
    name: locale === "zh-CN" ? "欢迎登录 DLR 控制台" : "Welcome to the DLR Console",
  })).toBeVisible();
  await page.getByTestId("admin-token-input").fill("fixture-token");
  await page.getByTestId("admin-token-submit").click();
  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
  await page.getByTestId("adapter-item").first().click();
  await expect(page.getByTestId("editor-main")).toBeVisible();
  await page.getByTestId("open-ai-assistant").click();
  await expect(page.getByTestId("ai-composer-guidance")).toBeVisible();

  const guidance = await readGuidance(page);
  if (width <= 1180) {
    expect(guidance.line_count).toBeGreaterThan(1);
    expect(guidance.flex_wrap).toBe("wrap");
    expect(guidance.hint_white_space).toBe("normal");
    expect(guidance.privacy_white_space).toBe("normal");
  } else {
    expect(guidance.line_count).toBe(1);
    expect(guidance.flex_wrap).toBe("nowrap");
    expect(guidance.hint_white_space).toBe("nowrap");
    expect(guidance.privacy_white_space).toBe("nowrap");
  }
  expect(guidance.hint_client_width).toBeGreaterThan(0);
  expect(guidance.privacy_client_width).toBeGreaterThan(0);
  expect(guidance.hint_scroll_width).toBeLessThanOrEqual(guidance.client_width);
  expect(guidance.hint_scroll_width).toBeLessThanOrEqual(guidance.hint_client_width);
  expect(guidance.privacy_scroll_width).toBeLessThanOrEqual(guidance.client_width);
  expect(guidance.privacy_scroll_width).toBeLessThanOrEqual(guidance.privacy_client_width);
  expect(guidance.scroll_width).toBeLessThanOrEqual(guidance.client_width);
  await expect(page.getByTestId("ai-composer-guidance").locator("br")).toHaveCount(0);
  await expect(page.getByTestId("ai-composer-guidance").locator(":scope > div, :scope > p")).toHaveCount(0);
  expect(guidance.text).toContain(locale === "zh-CN" ? "最多 8 个" : "max 8");
  expect(guidance.text).toContain(locale === "zh-CN" ? "敏感凭据" : "sensitive credentials");
  expect(guidance.hint_text).toContain("6 MiB");
  expect(guidance.hint_text).toContain("12 MiB");
  expect(guidance.privacy_text).toContain(
    locale === "zh-CN" ? "请勿上传密码/密钥/敏感凭据。" : "No passwords/keys or sensitive credentials.",
  );
  expect(guidance.hint_aria_label).toContain(
    locale === "zh-CN" ? "支持图片 / PDF / DOCX / 文本与代码文件" : "Images / PDF / DOCX / text and code files",
  );
  expect(guidance.guidance_aria_label).toContain(
    locale === "zh-CN" ? "附件内容会发送给管理员配置的模型服务" : "Attachment content is sent to the model service configured by the administrator",
  );
  expect(guidance.privacy_aria_label).toContain(
    locale === "zh-CN" ? "附件内容会发送给管理员配置的模型服务" : "Attachment content is sent to the model service configured by the administrator",
  );
  expect(guidance.privacy_aria_label).toContain(
    locale === "zh-CN" ? "请勿上传密码、密钥等敏感凭据" : "Do not upload passwords, keys or other sensitive credentials",
  );

  await page.getByTestId("ai-attachment-input").setInputFiles({
    name: "picker.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("picker fixture", "utf8"),
  });
  await expect(page.getByTestId("ai-attachment-item")).toHaveCount(1);
  const pickerCount = await page.getByTestId("ai-attachment-item").count();

  await addDroppedFixtureFile(page);
  await expect(page.getByTestId("ai-attachment-item")).toHaveCount(2);
  const dragCount = await page.getByTestId("ai-attachment-item").count();

  await page.getByTestId("ai-attachment-input").setInputFiles({
    name: "blocked.exe",
    mimeType: "application/x-msdownload",
    buffer: Buffer.from("blocked fixture", "utf8"),
  });
  await expect(page.getByTestId("ai-attachment-error")).toBeVisible();
  const invalidCount = await page.getByTestId("ai-attachment-item").count();
  expect(invalidCount).toBe(2);

  await page.getByTestId("ai-attachment-remove").first().click();
  await expect(page.getByTestId("ai-attachment-item")).toHaveCount(1);
  const afterRemoveCount = await page.getByTestId("ai-attachment-item").count();

  await page.getByTestId("ai-message-input").fill("fixture attachment request");
  await page.getByTestId("ai-send").click();
  await expect(page.getByTestId("ai-message-assistant")).toBeVisible();
  expect(assistPaths).toEqual(["POST /api/adapters/1/ai/assist"]);
  expect(nonGetPaths).toEqual(["POST /api/adapters/1/ai/assist"]);
  expect(unknownPaths).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);

  const overflow = await readOverflow(page);
  expect(overflow.document_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(overflow.body_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(overflow.document_scroll_height).toBeLessThanOrEqual(overflow.inner_height);
  expect(overflow.body_scroll_height).toBeLessThanOrEqual(overflow.inner_height);

  const screenshot = await takeScreenshot(page, locale + "-" + width + "-ai-attachments.png");
  records.push({
    locale,
    width,
    screenshot,
    guidance,
    overflow,
    attachment_lifecycle: {
      picker_count: pickerCount,
      drag_count: dragCount,
      invalid_count: invalidCount,
      after_remove_count: afterRemoveCount,
      error_visible: true,
    },
    requests: {
      assist_count: assistPaths.length,
      assist_paths: assistPaths,
      non_get_paths: nonGetPaths,
      unknown_paths: unknownPaths,
    },
    console_errors: consoleErrors,
    page_errors: pageErrors,
  });
}

test.afterAll(() => {
  mkdirSync(evidenceRoot, { recursive: true });
  records.sort((left, right) =>
    left.locale.localeCompare(right.locale) || left.width - right.width,
  );
  writeFileSync(
    resolve(evidenceRoot, "browser-report.json"),
    JSON.stringify({
      schema_version: 1,
      product: "DataLinkRuntime",
      dispatch_id: "issue117-b6-ai-20260825-r1",
      batch: "6",
      scope: "AI Assistant attachment guidance and adjacent attachment behavior",
      antd: "5.29.3",
      playwright: "1.62.1",
      browser: "chromium",
      browser_version: browserVersion,
      viewport_widths: VIEWPORTS,
      viewport_height: 900,
      locales: LOCALES,
      fixture_provider: "fixture",
      real_provider_credentials: false,
      raw_provider_response_archived: false,
      records,
    }, null, 2) + "\n",
    "utf8",
  );
});

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(locale + " " + width + "px AI attachment one-line contract", async ({ browser }) => {
      browserVersion = browser.version();
      const context = await browser.newContext({ locale, viewport: { width, height: 900 } });
      const page = await context.newPage();
      await runCase(page, locale, width);
      await page.close();
      await context.close();
    });
  }
}
