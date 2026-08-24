import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page, type Route } from "@playwright/test";

type Locale = "zh-CN" | "en";

const LOCALES: readonly Locale[] = ["zh-CN", "en"];
const VIEWPORTS = [1280, 1440, 1680, 1920] as const;
const LIFECYCLE_PATH_PARTS = ["/versions", "/credential-bindings", "/executions", "/schedule", "/webhook"] as const;
const specDir = dirname(fileURLToPath(import.meta.url));
const evidenceRoot = resolve(
  specDir,
  process.env.DLR_ISSUE117_B5_OUTPUT_DIR ?? "../../../docs/evidence/issue117-b5/auxiliary-matrix",
);
const screenshotDir = resolve(evidenceRoot, "browser");

const adapter = {
  id: 1,
  name: "Issue 117 Batch 5 fixture adapter",
  description: "Deterministic fixture for editor layout regression checks.",
  language: "python",
  adapter_type: "task",
  run_mode: "manual",
  timeout_seconds: 300,
  owner_user_id: 42,
  owner_username: "fixture-owner",
  latest_version_id: 10,
  runtime_worker_id: 1,
  runtime_locked: false,
  archived_at: null,
  running_execution_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  access_level: "admin",
};

const version = {
  id: 10,
  adapter_id: 1,
  seq: 1,
  created_at: "2026-01-01T00:00:00Z",
  code: Array.from({ length: 80 }, (_, index) => `line_${index + 1} = ${index + 1}`).join("\n") + "\n",
  requirements: "requests==2.32.0\n",
  runtime_config: {},
};

const worker = {
  id: 1,
  name: "fixture-worker",
  status: "online",
  last_heartbeat: "2026-01-01T00:00:00Z",
  capabilities: ["python", "javascript", "java"],
};

const credential = {
  id: 7,
  name: "fixture-credential",
  type: "token",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const binding = {
  env_key: "API_TOKEN",
  credential_id: credential.id,
  field: "token",
  credential_name: credential.name,
  credential_type: credential.type,
};

interface LayoutState {
  selection_start_line: number;
  selection_start_column: number;
  selection_end_line: number;
  selection_end_column: number;
  top_visible_line: number;
}

interface RequestRecord {
  method: string;
  path: string;
}

interface BrowserRecord {
  locale: Locale;
  width: number;
  screenshots: string[];
  initial: LayoutState;
  maximized: LayoutState;
  before_restore: LayoutState;
  restored: LayoutState;
  before_escape: LayoutState;
  escaped: LayoutState;
  lifecycle_requests: RequestRecord[];
  unknown_requests: RequestRecord[];
  console_errors: string[];
  page_errors: string[];
  overflow: {
    normal: { horizontal: boolean; vertical: boolean };
    maximized: { horizontal: boolean; vertical: boolean };
    restored: { horizontal: boolean; vertical: boolean };
  };
}

const records: BrowserRecord[] = [];
let browserVersion = "unknown";

function jsonBody(body: unknown): string {
  return JSON.stringify(body);
}

async function fulfillJson(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: jsonBody(body),
  });
}

function errorBody(code: string, message: string) {
  return { detail: { code, message } };
}

async function installRoutes(page: Page, locale: Locale) {
  const requestLog: RequestRecord[] = [];
  const unknownRequests: RequestRecord[] = [];

  await page.route("**/entry-mode.js", async (route) => {
    await route.fulfill({
      contentType: "application/javascript",
      body: 'window.__DLR_ENTRY_MODE__ = "token";',
    });
  });

  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/")) {
      requestLog.push({ method: request.method(), path: `${url.pathname}${url.search}` });
    }
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === "/api/locale" && method === "GET") {
      await fulfillJson(route, { locale });
      return;
    }
    if (path === "/api/health" && method === "GET") {
      await fulfillJson(route, { status: "ok", database: true });
      return;
    }
    if (path === "/api/auth/admin/verify" && method === "GET") {
      await fulfillJson(route, { status: "ok" });
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
      await fulfillJson(route, [{ id: version.id, adapter_id: version.adapter_id, seq: version.seq, created_at: version.created_at }]);
      return;
    }
    if (path === "/api/adapters/1/versions/10" && method === "GET") {
      await fulfillJson(route, version);
      return;
    }
    if (path === "/api/adapters/1/schedule" && method === "GET") {
      await fulfillJson(route, {
        adapter_id: 1,
        enabled: false,
        cron: "0 * * * *",
        timezone: "UTC",
        input: {},
        next_run_at: null,
        updated_at: "2026-01-01T00:00:00Z",
      });
      return;
    }
    if (path === "/api/adapters/1/executions" && method === "GET") {
      await fulfillJson(route, { items: [], next_before_id: null });
      return;
    }
    if (path === "/api/credentials" && method === "GET") {
      await fulfillJson(route, [credential]);
      return;
    }
    if (path === "/api/adapters/1/credential-bindings" && method === "GET") {
      await fulfillJson(route, [binding]);
      return;
    }
    if (path === "/api/adapters/1/credential-options" && method === "GET") {
      await fulfillJson(route, [credential]);
      return;
    }
    if (path === "/api/ai/attachment-capabilities" && method === "GET") {
      await fulfillJson(route, {
        limits: { max_attachments: 5, max_file_bytes: 2_000_000, max_total_bytes: 5_000_000 },
        supported_content_types: ["text/plain", "text/markdown", "text/x-python", "application/json"],
      });
      return;
    }
    if (path === "/api/adapters/1/ai/knowledge-capability" && method === "GET") {
      await fulfillJson(route, { available: false, reason: "fixture_knowledge_source_disabled" });
      return;
    }

    const unknown = { method, path: `${path}${url.search}` };
    unknownRequests.push(unknown);
    await fulfillJson(route, errorBody("issue117_b5_unhandled_request", `${method} ${path}`), 404);
  });

  return { requestLog, unknownRequests };
}

function readLayoutState(page: Page): Promise<LayoutState> {
  return page.getByTestId("editor-main").evaluate((element) => {
    const value = (name: string): number => {
      const raw = element.getAttribute(name);
      if (raw === null) {
        throw new Error(`Missing editor layout attribute ${name}`);
      }
      const parsed = Number(raw);
      if (!Number.isFinite(parsed)) {
        throw new Error(`Invalid editor layout attribute ${name}: ${raw}`);
      }
      return parsed;
    };
    return {
      selection_start_line: value("data-selection-start-line"),
      selection_start_column: value("data-selection-start-column"),
      selection_end_line: value("data-selection-end-line"),
      selection_end_column: value("data-selection-end-column"),
      top_visible_line: value("data-top-visible-line"),
    };
  });
}

async function waitForLayoutState(page: Page, expected: LayoutState): Promise<void> {
  await expect.poll(() => readLayoutState(page), { timeout: 10_000 }).toEqual(expected);
}

async function readOverflow(page: Page) {
  return page.evaluate(() => {
    const root = document.documentElement;
    const body = document.body;
    return {
      horizontal: root.scrollWidth > root.clientWidth || body.scrollWidth > window.innerWidth,
      vertical: root.scrollHeight > root.clientHeight || body.scrollHeight > window.innerHeight,
    };
  });
}

async function captureScreenshot(page: Page, name: string): Promise<string> {
  mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({ path: resolve(screenshotDir, name), fullPage: true, animations: "disabled" });
  return `docs/evidence/issue117-b5/auxiliary-matrix/browser/${name}`;
}

async function loginAndOpenEditor(page: Page, locale: Locale): Promise<void> {
  await page.goto("/");
  await expect(page.getByRole("heading", {
    name: locale === "zh-CN" ? "欢迎登录 DLR 控制台" : "Welcome to the DLR Console",
  })).toBeVisible();
  await page.getByTestId("admin-token-input").fill("FIXTURE_TOKEN");
  await page.getByTestId("admin-token-submit").click();
  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
  await page.getByTestId("adapter-item").click();
  await expect(page.getByTestId("workbench-header")).toBeVisible();
  await expect(page.getByTestId("editor-main")).toBeVisible();
}

async function setKnownSelection(page: Page, expected: LayoutState): Promise<void> {
  const input = page.getByTestId("editor-main").getByRole("textbox");
  await expect(input).toBeAttached();
  await input.focus();
  // Avoid platform-specific Home/Meta mappings: a bounded sequence of real
  // cursor keys deterministically reaches the first line/column on every
  // Chromium host, then creates the exact five-field selection.
  for (let index = 0; index < 120; index += 1) {
    await page.keyboard.press("ArrowUp");
    await page.keyboard.press("ArrowLeft");
  }
  for (let index = 0; index < expected.selection_start_line - 1; index += 1) {
    await page.keyboard.press("ArrowDown");
  }
  for (let index = 0; index < expected.selection_start_column - 1; index += 1) {
    await page.keyboard.press("ArrowRight");
  }
  for (let index = expected.selection_start_line; index < expected.selection_end_line; index += 1) {
    await page.keyboard.press("Shift+ArrowDown");
  }
  for (let index = expected.selection_start_column; index < expected.selection_end_column; index += 1) {
    await page.keyboard.press("Shift+ArrowRight");
  }
  try {
    await waitForLayoutState(page, expected);
  } catch {
    const currentAttributes = await page.getByTestId("editor-main").evaluate((element) => ({
      startLine: element.getAttribute("data-selection-start-line"),
      startColumn: element.getAttribute("data-selection-start-column"),
      endLine: element.getAttribute("data-selection-end-line"),
      endColumn: element.getAttribute("data-selection-end-column"),
      topLine: element.getAttribute("data-top-visible-line"),
    }));
    throw new Error(`Unable to establish the deterministic Monaco selection: ${JSON.stringify(currentAttributes)}`);
  }
}

async function runCase(page: Page, locale: Locale, width: number) {
  const { requestLog, unknownRequests } = await installRoutes(page, locale);
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await loginAndOpenEditor(page, locale);
  const labels = locale === "zh-CN"
    ? { maximize: "最大化代码编辑器", restore: "恢复代码编辑器布局" }
    : { maximize: "Maximize code editor", restore: "Restore code editor layout" };
  await expect(page.getByTestId("editor-maximize")).toHaveAccessibleName(labels.maximize);
  await expect(page.getByTestId("requirements-collapse-header")).toBeVisible();
  await expect(page.getByTestId("bindings-collapse-header")).toBeVisible();
  await expect(page.locator(".ant-collapse-item").nth(0)).not.toHaveClass(/ant-collapse-item-active/);
  await expect(page.locator(".ant-collapse-item").nth(1)).not.toHaveClass(/ant-collapse-item-active/);

  await page.getByTestId("requirements-collapse-header").click();
  await expect(page.getByTestId("requirements-input")).toBeVisible();
  await page.getByTestId("requirements-input").fill("httpx==0.28.0\n");
  await page.getByTestId("bindings-collapse-header").click();
  await expect(page.getByTestId("credential-bindings")).toBeVisible();
  await page.getByTestId("binding-env-key").fill("RENAMED_TOKEN");
  await page.getByTestId("requirements-collapse-header").click();
  await page.getByTestId("bindings-collapse-header").click();
  await page.getByTestId("requirements-collapse-header").click();
  await page.getByTestId("bindings-collapse-header").click();
  await expect(page.getByTestId("requirements-input")).toHaveValue("httpx==0.28.0\n");
  await expect(page.getByTestId("binding-env-key")).toHaveValue("RENAMED_TOKEN");

  const expectedInitial: LayoutState = {
    selection_start_line: 4,
    selection_start_column: 3,
    selection_end_line: 8,
    selection_end_column: 9,
    top_visible_line: 1,
  };
  await setKnownSelection(page, expectedInitial);
  await page.mouse.wheel(0, 420);
  await expect.poll(() => readLayoutState(page)).toMatchObject({
    selection_start_line: expectedInitial.selection_start_line,
    selection_start_column: expectedInitial.selection_start_column,
    selection_end_line: expectedInitial.selection_end_line,
    selection_end_column: expectedInitial.selection_end_column,
  });
  const initial = await readLayoutState(page);
  await expect(page.getByTestId("editor-main")).toHaveAttribute("data-layout", "normal");
  const normalOverflow = await readOverflow(page);
  const normalRect = await page.getByTestId("editor-main").boundingBox();
  const screenshots = [await captureScreenshot(page, `${locale}-${width}-collapsed.png`)];

  await page.getByTestId("editor-maximize").click();
  await expect(page.getByTestId("editor-restore")).toBeVisible();
  await expect(page.getByTestId("editor-restore")).toHaveAccessibleName(labels.restore);
  await waitForLayoutState(page, initial);
  const maximized = await readLayoutState(page);
  expect(maximized).toEqual(initial);
  await expect(page.getByTestId("editor-restore")).toBeFocused();
  const maximizedRect = await page.getByTestId("editor-main").boundingBox();
  expect(maximizedRect?.height ?? 0).toBeGreaterThan(normalRect?.height ?? 0);
  screenshots.push(await captureScreenshot(page, `${locale}-${width}-maximized.png`));
  const maximizedOverflow = await readOverflow(page);

  const input = page.getByTestId("editor-main").getByRole("textbox");
  await input.focus();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.type("edited while maximized\n");
  await expect(page.getByTestId("save-version")).toBeEnabled();
  await expect(page.locator(".monaco-editor .view-lines")).toContainText("edited while maximized");
  const beforeRestore = await readLayoutState(page);
  await page.getByTestId("editor-restore").click();
  await expect(page.getByTestId("editor-maximize")).toBeVisible();
  await waitForLayoutState(page, beforeRestore);
  const restored = await readLayoutState(page);
  expect(restored).toEqual(beforeRestore);
  await expect(page.getByTestId("editor-maximize")).toBeFocused();
  const restoredOverflow = await readOverflow(page);
  screenshots.push(await captureScreenshot(page, `${locale}-${width}-restored.png`));

  await page.getByTestId("editor-maximize").click();
  await expect(page.getByTestId("editor-restore")).toBeVisible();
  const beforeEscape = await readLayoutState(page);
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("editor-maximize")).toBeVisible();
  await waitForLayoutState(page, beforeEscape);
  const escaped = await readLayoutState(page);
  expect(escaped).toEqual(beforeEscape);
  await expect(page.getByTestId("editor-maximize")).toBeFocused();

  const lifecycleRequests = requestLog.filter((request) =>
    request.method !== "GET" &&
    LIFECYCLE_PATH_PARTS.some((part) => request.path.includes(part)),
  );
  expect(lifecycleRequests).toEqual([]);
  expect(unknownRequests).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
  records.push({
    locale,
    width,
    screenshots,
    initial,
    maximized,
    before_restore: beforeRestore,
    restored,
    before_escape: beforeEscape,
    escaped,
    lifecycle_requests: lifecycleRequests,
    unknown_requests: unknownRequests,
    console_errors: consoleErrors,
    page_errors: pageErrors,
    overflow: {
      normal: normalOverflow,
      maximized: maximizedOverflow,
      restored: restoredOverflow,
    },
  });
}

test.afterAll(() => {
  mkdirSync(evidenceRoot, { recursive: true });
  writeFileSync(
    resolve(evidenceRoot, "browser-report.json"),
    `${JSON.stringify({
      schema_version: 1,
      product: "DataLinkRuntime",
      dispatch_id: "issue117-b5-editor-20260824-r1",
      scope: "Issue 117 Batch 5 editor configuration collapse and Monaco maximize/restore",
      antd: "5.29.3",
      pro_components: "2.8.10",
      browser: "chromium",
      browser_version: browserVersion,
      node_version: process.version,
      viewport_widths: VIEWPORTS,
      viewport_height: 900,
      locales: LOCALES,
      fake_provider: "fixture",
      real_provider_credentials: false,
      records,
    }, null, 2)}\n`,
    "utf8",
  );
});

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`Issue 117 Batch 5 editor ${locale} ${width}px`, async ({ browser }) => {
      browserVersion = browser.version();
      const context = await browser.newContext({ locale, viewport: { width, height: 900 } });
      const page = await context.newPage();
      await runCase(page, locale, width);
      await page.close();
      await context.close();
    });
  }
}
