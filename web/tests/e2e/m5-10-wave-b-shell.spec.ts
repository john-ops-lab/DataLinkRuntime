import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page, type Route } from "@playwright/test";

type Locale = "zh-CN" | "en";
type ConsoleScenario = "admin" | "read" | "empty" | "error";

const LOCALES: readonly Locale[] = ["zh-CN", "en"];
const VIEWPORTS = [1280, 1440, 1680, 1920] as const;
const specDir = dirname(fileURLToPath(import.meta.url));
const evidenceRoot = resolve(
  specDir,
  process.env.DLR_WAVE_B_OUTPUT_DIR ?? "../../../docs/ui/m5-10-wave-b",
);
const screenshotDir = resolve(evidenceRoot, "browser");

const adapter = {
  id: 1,
  name: "Orders to Warehouse — 长名称 Long Adapter Name",
  description: "Wave B shell fixture with intentionally long copy for responsive checks.",
  language: "python",
  adapter_type: "task",
  run_mode: "manual",
  timeout_seconds: 300,
  owner_user_id: 42,
  owner_username: "owner-with-a-long-display-name",
  latest_version_id: null,
  runtime_worker_id: 1,
  runtime_locked: false,
  archived_at: null,
  running_execution_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const worker = {
  id: 1,
  name: "runtime-worker-with-a-long-display-name",
  status: "online",
  last_heartbeat: "2026-01-01T00:00:00Z",
  capabilities: ["python", "javascript", "java"],
};

interface BrowserRecord {
  locale: Locale;
  width: number;
  scenario: ConsoleScenario | "loading";
  screenshot: string;
  overflow: {
    inner_width: number;
    document_scroll_width: number;
    body_scroll_width: number;
  };
  console_errors: string[];
  expected_console_errors: string[];
  page_errors: string[];
  unknown_requests: string[];
  visible_states: Record<string, boolean>;
}

const records: BrowserRecord[] = [];

function jsonBody(body: unknown): string {
  return JSON.stringify(body);
}

async function fulfillJson(route: Route, body: unknown, status = 200, headers?: Record<string, string>) {
  await route.fulfill({
    status,
    contentType: "application/json",
    headers,
    body: jsonBody(body),
  });
}

function errorBody(code: string, message: string) {
  return { detail: { code, message } };
}

function captureDiagnostics(page: Page, scenario: ConsoleScenario | "loading") {
  const consoleErrors: string[] = [];
  const expectedConsoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() !== "error") {
      return;
    }
    const text = message.text();
    const expectedHttpError =
      (scenario === "read" || scenario === "loading") && text.includes("status of 401") ||
      scenario === "error" && text.includes("status of 503");
    if (expectedHttpError) {
      expectedConsoleErrors.push(text);
    } else {
      consoleErrors.push(text);
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  return { consoleErrors, expectedConsoleErrors, pageErrors };
}

async function installRoutes(
  page: Page,
  scenario: ConsoleScenario,
  locale: Locale,
): Promise<{ unknownRequests: string[] }> {
  let accountLoggedIn = false;
  const unknownRequests: string[] = [];

  await page.route("**/entry-mode.js", async (route) => {
    await route.fulfill({
      contentType: "application/javascript",
      body: `window.__DLR_ENTRY_MODE__ = "${scenario === "read" ? "account" : "token"}";`,
    });
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
    if (path === "/api/workers" && method === "GET") {
      await fulfillJson(route, [worker]);
      return;
    }
    if (path === "/api/adapters" && method === "GET") {
      if (scenario === "empty") {
        await fulfillJson(route, []);
      } else if (scenario === "error") {
        await fulfillJson(route, errorBody("adapter_list_failed", "fixture failure"), 503);
      } else {
        await fulfillJson(route, [{ ...adapter, access_level: scenario === "read" ? "read" : "admin" }]);
      }
      return;
    }
    if (path === "/api/adapters/1/versions" && method === "GET") {
      await fulfillJson(route, []);
      return;
    }
    if (path === "/api/auth/admin/verify" && method === "GET" && scenario !== "read") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/auth/account/csrf" && method === "GET" && scenario === "read") {
      await fulfillJson(route, { status: "ok" }, 200, {
        "set-cookie": "dlr_account_csrf=wave-b-csrf; Path=/",
      });
      return;
    }
    if (path === "/api/auth/account/me" && method === "GET" && scenario === "read") {
      if (accountLoggedIn) {
        await fulfillJson(route, {
          principal: {
            id: 7,
            username: "reader-user-with-a-long-name",
            role: "user",
            enabled: true,
            must_change_password: false,
          },
        });
      } else {
        await fulfillJson(route, errorBody("account_session_required", "Account Session is required"), 401);
      }
      return;
    }
    if (path === "/api/auth/account/login" && method === "POST" && scenario === "read") {
      accountLoggedIn = true;
      await fulfillJson(route, {
        principal: {
          id: 7,
          username: "reader-user-with-a-long-name",
          role: "user",
          enabled: true,
          must_change_password: false,
        },
      });
      return;
    }

    const requestKey = `${method} ${path}${url.search}`;
    unknownRequests.push(requestKey);
    await fulfillJson(route, errorBody("wave_b_unhandled_request", requestKey), 404);
  });

  return { unknownRequests };
}

async function collectShellRecord(
  page: Page,
  locale: Locale,
  width: number,
  scenario: ConsoleScenario,
  unknownRequests: string[],
) {
  const diagnostics = captureDiagnostics(page, scenario);

  const labels = locale === "zh-CN"
    ? { edit: "编辑", readonly: "只读访问" }
    : { edit: "Edit", readonly: "Read-only access" };
  const visibleStates: Record<string, boolean> = {};

  await page.goto("/");
  if (scenario === "read") {
    await expect(page.getByRole("heading", { name: locale === "zh-CN" ? "账号登录" : "Account login" })).toBeVisible();
    await page.getByTestId("account-username-input").fill("reader-user");
    await page.getByTestId("account-password-input").fill("fixture-password");
    await page.getByTestId("account-login-submit").click();
  } else {
    await expect(page.getByRole("heading", { name: locale === "zh-CN" ? "欢迎登录 DLR 控制台" : "Welcome to the DLR Console" })).toBeVisible();
    await page.getByTestId("admin-token-input").fill("fixture-token");
    await page.getByTestId("admin-token-submit").click();
  }

  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
  await expect(page.getByTestId("app-header")).toBeVisible();
  await expect(page.locator(".catalog-title")).toBeVisible();
  visibleStates.pro_layout = false;
  visibleStates.page_container = false;
  visibleStates.top_bar = await page.getByTestId("app-header").isVisible();
  visibleStates.navigation = false;

  await page.getByTestId("adapter-item").first().click();
  await expect(page.getByTestId("workbench-header")).toBeVisible();
  await expect(page.getByTestId("workbench-header")).toContainText(adapter.name);
  visibleStates.workbench_tabs = await page.getByRole("tab", { name: labels.edit }).isVisible();
  if (scenario === "read") {
    await expect(page.getByTestId("adapter-read-only")).toBeVisible();
    await expect(page.getByTestId("adapter-read-only-notice")).toContainText(labels.readonly);
    visibleStates.permission_denied_read_only = await page.getByTestId("adapter-read-only-notice").isVisible();
  } else {
    visibleStates.permission_denied_read_only = false;
  }

  await page.getByRole("tab", { name: labels.edit }).focus();
  expect(await page.evaluate(() => document.activeElement?.getAttribute("role"))).toBe("tab");
  if (scenario === "read") {
    await page.getByTestId("user-menu").click();
    expect(await page.getByTestId("account-profile").getAttribute("aria-label")).not.toBeNull();
    await page.keyboard.press("Escape");
  }

  const overflow = await page.evaluate(() => ({
    inner_width: window.innerWidth,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
  }));
  expect(overflow.document_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(overflow.body_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(unknownRequests).toEqual([]);
  expect(diagnostics.consoleErrors).toEqual([]);
  expect(diagnostics.pageErrors).toEqual([]);

  mkdirSync(screenshotDir, { recursive: true });
  const screenshotName = `${locale}-${width}-${scenario}.png`;
  await page.screenshot({
    path: resolve(screenshotDir, screenshotName),
    fullPage: true,
    animations: "disabled",
  });
  records.push({
    locale,
    width,
    scenario,
    screenshot: `docs/ui/m5-10-wave-b/browser/${screenshotName}`,
    overflow,
    console_errors: diagnostics.consoleErrors,
    expected_console_errors: diagnostics.expectedConsoleErrors,
    page_errors: diagnostics.pageErrors,
    unknown_requests: unknownRequests,
    visible_states: visibleStates,
  });
}

test.afterAll(() => {
  mkdirSync(evidenceRoot, { recursive: true });
  writeFileSync(
    resolve(evidenceRoot, "browser-report.json"),
    `${JSON.stringify({
      schema_version: 1,
      product: "DataLinkRuntime",
      wave: "M5.10 Wave B",
      antd: "5.29.3",
      pro_components: "2.8.10",
      playwright: "1.62.1",
      browser: "chromium",
      viewport_widths: VIEWPORTS,
      viewport_height: 900,
      locales: LOCALES,
      records,
    }, null, 2)}\n`,
    "utf8",
  );
});

test.beforeAll(() => {
  mkdirSync(screenshotDir, { recursive: true });
});

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`${locale} ${width}px shell and read-only audit`, async ({ browser }) => {
      const context = await browser.newContext({ locale, viewport: { width, height: 900 } });
      const adminPage = await context.newPage();
      const adminRoutes = await installRoutes(adminPage, "admin", locale);
      await collectShellRecord(adminPage, locale, width, "admin", adminRoutes.unknownRequests);
      await adminPage.close();

      const readPage = await context.newPage();
      const readRoutes = await installRoutes(readPage, "read", locale);
      await collectShellRecord(readPage, locale, width, "read", readRoutes.unknownRequests);
      await readPage.close();
      await context.close();
    });
  }
}

test("Wave B records loading, empty, error, and disabled states without layout overflow", async ({ browser }) => {
  const context = await browser.newContext({ locale: "zh-CN", viewport: { width: 1280, height: 900 } });

  const emptyPage = await context.newPage();
  const emptyRoutes = await installRoutes(emptyPage, "empty", "zh-CN");
  const emptyDiagnostics = captureDiagnostics(emptyPage, "empty");
  await emptyPage.goto("/");
  await emptyPage.getByTestId("admin-token-input").fill("fixture-token");
  await emptyPage.getByTestId("admin-token-submit").click();
  await expect(emptyPage.locator(".ant-empty.catalog-empty")).toBeVisible();
  await expect(emptyPage.getByTestId("workbench-empty").locator(".ant-result")).toBeVisible();
  await emptyPage.screenshot({ path: resolve(screenshotDir, "zh-CN-1280-empty.png"), fullPage: true, animations: "disabled" });
  expect(emptyRoutes.unknownRequests).toEqual([]);
  expect(emptyDiagnostics.consoleErrors).toEqual([]);
  expect(emptyDiagnostics.pageErrors).toEqual([]);
  const emptyOverflow = await emptyPage.evaluate(() => ({
    inner_width: window.innerWidth,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
  }));
  expect(emptyOverflow.document_scroll_width).toBeLessThanOrEqual(emptyOverflow.inner_width);
  records.push({
    locale: "zh-CN",
    width: 1280,
    scenario: "empty",
    screenshot: "docs/ui/m5-10-wave-b/browser/zh-CN-1280-empty.png",
    overflow: emptyOverflow,
    console_errors: [],
    expected_console_errors: emptyDiagnostics.expectedConsoleErrors,
    page_errors: emptyDiagnostics.pageErrors,
    unknown_requests: emptyRoutes.unknownRequests,
    visible_states: { empty_catalog: true, empty_workbench: true },
  });
  await emptyPage.close();

  const errorPage = await context.newPage();
  const errorRoutes = await installRoutes(errorPage, "error", "zh-CN");
  const errorDiagnostics = captureDiagnostics(errorPage, "error");
  await errorPage.goto("/");
  await errorPage.getByTestId("admin-token-input").fill("fixture-token");
  await errorPage.getByTestId("admin-token-submit").click();
  await expect(errorPage.getByTestId("error-banner")).toBeVisible();
  await expect(errorPage.getByTestId("error-banner")).toContainText("请求失败");
  await errorPage.screenshot({ path: resolve(screenshotDir, "zh-CN-1280-error.png"), fullPage: true, animations: "disabled" });
  expect(errorRoutes.unknownRequests).toEqual([]);
  expect(errorDiagnostics.consoleErrors).toEqual([]);
  expect(errorDiagnostics.pageErrors).toEqual([]);
  const errorOverflow = await errorPage.evaluate(() => ({
    inner_width: window.innerWidth,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
  }));
  expect(errorOverflow.document_scroll_width).toBeLessThanOrEqual(errorOverflow.inner_width);
  records.push({
    locale: "zh-CN",
    width: 1280,
    scenario: "error",
    screenshot: "docs/ui/m5-10-wave-b/browser/zh-CN-1280-error.png",
    overflow: errorOverflow,
    console_errors: [],
    expected_console_errors: errorDiagnostics.expectedConsoleErrors,
    page_errors: errorDiagnostics.pageErrors,
    unknown_requests: errorRoutes.unknownRequests,
    visible_states: { error_feedback: true },
  });
  await errorPage.close();

  const loadingPage = await context.newPage();
  const loadingDiagnostics = captureDiagnostics(loadingPage, "loading");
  const loadingUnknownRequests: string[] = [];
  let releaseBootstrap: (() => void) | undefined;
  const bootstrapBlocked = new Promise<void>((resolvePromise) => {
    releaseBootstrap = resolvePromise;
  });
  await loadingPage.route("**/entry-mode.js", async (route) => {
    await route.fulfill({ contentType: "application/javascript", body: 'window.__DLR_ENTRY_MODE__ = "account";' });
  });
  await loadingPage.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/locale" || path === "/api/auth/account/csrf") {
      await fulfillJson(route, { locale: "zh-CN", status: "ok" }, 200, path.endsWith("csrf") ? { "set-cookie": "dlr_account_csrf=wave-b-csrf; Path=/" } : undefined);
      return;
    }
    if (path === "/api/auth/account/me") {
      await bootstrapBlocked;
      await fulfillJson(route, errorBody("account_session_required", "expired"), 401);
      return;
    }
    loadingUnknownRequests.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
    await fulfillJson(route, []);
  });
  await loadingPage.goto("/");
  await expect(loadingPage.locator(".account-loading .ant-skeleton")).toBeVisible();
  await loadingPage.screenshot({ path: resolve(screenshotDir, "zh-CN-1280-loading.png"), fullPage: true, animations: "disabled" });
  const loadingOverflow = await loadingPage.evaluate(() => ({
    inner_width: window.innerWidth,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
  }));
  expect(loadingDiagnostics.consoleErrors).toEqual([]);
  expect(loadingDiagnostics.pageErrors).toEqual([]);
  expect(loadingUnknownRequests).toEqual([]);
  expect(loadingOverflow.document_scroll_width).toBeLessThanOrEqual(loadingOverflow.inner_width);
  records.push({
    locale: "zh-CN",
    width: 1280,
    scenario: "loading",
    screenshot: "docs/ui/m5-10-wave-b/browser/zh-CN-1280-loading.png",
    overflow: loadingOverflow,
    console_errors: [],
    expected_console_errors: loadingDiagnostics.expectedConsoleErrors,
    page_errors: loadingDiagnostics.pageErrors,
    unknown_requests: loadingUnknownRequests,
    visible_states: { loading_skeleton: true },
  });
  releaseBootstrap?.();
  await loadingPage.close();
  await context.close();
});
