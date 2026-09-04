import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page, type Route } from "@playwright/test";

type BaselineLocale = "zh-CN" | "en";
type BaselineScenario = "token" | "account-read";

const LOCALES: readonly BaselineLocale[] = ["zh-CN", "en"];
const VIEWPORTS = [1280, 1440, 1680, 1920] as const;
const baselineSpecDir = dirname(fileURLToPath(import.meta.url));

const baselineOutputDir = resolve(
  baselineSpecDir,
  process.env.DLR_BASELINE_OUTPUT_DIR ?? "../../../docs/ui/m5-10-wave-a/baseline",
);

const adapter = {
  id: 1,
  name: "Orders to Warehouse",
  description: "Wave A browser fixture",
  language: "python",
  adapter_type: "task",
  run_mode: "manual",
  timeout_seconds: 300,
  owner_user_id: 42,
  owner_username: "owner-user",
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
  name: "baseline-worker",
  status: "online",
  last_heartbeat: "2026-01-01T00:00:00Z",
  capabilities: ["python", "javascript", "java"],
  protocol_version: 3,
  isolation_preflight_status: "passed",
  isolation_preflight_at: "2026-01-01T00:00:00Z",
  rabbitmq_execution_v3: true,
  isolation_capabilities: {
    cgroup_v2: true,
    mount_namespace: true,
    pid_namespace: true,
    memory_hard_limit: true,
    pids_hard_limit: true,
    tmpfs_hard_limit: true,
    bounded_output: true,
  },
};

interface BaselineRecord {
  locale: BaselineLocale;
  width: number;
  scenario: BaselineScenario;
  screenshot: string;
  console_errors: string[];
  expected_console_errors: string[];
  unexpected_console_errors: string[];
  page_errors: string[];
  http_errors: Array<{ path: string; status: number }>;
  unexpected_http_errors: Array<{ path: string; status: number }>;
  unknown_requests: string[];
  overflow: {
    inner_width: number;
    document_scroll_width: number;
    body_scroll_width: number;
  };
  visible_checkpoints: Record<string, boolean>;
}

const records: BaselineRecord[] = [];
let browserVersion = "unknown";

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

async function installRoutes(
  page: Page,
  scenario: BaselineScenario,
  locale: BaselineLocale,
): Promise<{ unknownRequests: string[] }> {
  let accountLoggedIn = false;
  const unknownRequests: string[] = [];

  await page.route("**/entry-mode.js", async (route) => {
    const mode = scenario === "account-read" ? "account" : "token";
    await route.fulfill({
      contentType: "application/javascript",
      body: `window.__DLR_ENTRY_MODE__ = "${mode}";`,
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
    if (path === "/api/system/managed-input-capability" && method === "GET") {
      await fulfillJson(route, {
        managed_files_enabled: false,
        ready: false,
        default_retention_seconds: 86_400,
        max_custom_retention_seconds: 2_592_000,
        allow_manual_delete: true,
        allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"],
      });
      return;
    }
    if (path === "/api/adapters" && method === "GET") {
      await fulfillJson(route, [{
        ...adapter,
        access_level: scenario === "account-read" ? "read" : "admin",
      }]);
      return;
    }
    if (path === "/api/adapters/1/versions" && method === "GET") {
      await fulfillJson(route, []);
      return;
    }
    if (path === "/api/adapters/1/schedule" && method === "GET") {
      await fulfillJson(route, errorBody("schedule_not_configured", "Schedule is not configured"), 404);
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
    if (path === "/api/adapters/1/input-config" && method === "GET") {
      await fulfillJson(route, {
        adapter_id: 1,
        revision: 1,
        source_type: "none",
        json_value: null,
        retention: { mode: "system_default", seconds: null },
        artifacts: [],
        valid_for_run: true,
        invalid_reason: null,
      });
      return;
    }
    if (path === "/api/auth/admin/verify" && method === "GET" && scenario === "token") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/auth/account/csrf" && method === "GET" && scenario === "account-read") {
      await fulfillJson(route, { status: "ok" }, 200, {
        "set-cookie": "dlr_account_csrf=baseline-csrf; Path=/",
      });
      return;
    }
    if (path === "/api/auth/account/me" && method === "GET" && scenario === "account-read") {
      if (accountLoggedIn) {
        await fulfillJson(route, {
          principal: {
            id: 7,
            username: "reader-user",
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
    if (path === "/api/auth/account/login" && method === "POST" && scenario === "account-read") {
      accountLoggedIn = true;
      await fulfillJson(route, {
        principal: {
          id: 7,
          username: "reader-user",
          role: "user",
          enabled: true,
          must_change_password: false,
        },
      });
      return;
    }

    const requestKey = `${method} ${path}${url.search}`;
    unknownRequests.push(requestKey);
    await fulfillJson(route, errorBody("baseline_unhandled_request", requestKey), 404);
  });

  return { unknownRequests };
}

function headingFor(locale: BaselineLocale, scenario: BaselineScenario): string {
  if (scenario === "account-read") {
    return locale === "zh-CN" ? "账号登录" : "Account login";
  }
  return locale === "zh-CN" ? "欢迎登录 DLR 控制台" : "Welcome to the DLR Console";
}

async function chooseLoginLocale(page: Page, locale: BaselineLocale): Promise<void> {
  await page.getByTestId("login-locale-select").click();
  const dropdown = page.locator(".ant-select-dropdown:visible");
  await expect(dropdown).toBeVisible();
  await dropdown.locator(".ant-select-item-option").filter({
    hasText: locale === "zh-CN" ? "简体中文" : "English",
  }).click();
}

async function collectErrors(page: Page): Promise<{ consoleErrors: string[]; pageErrors: string[] }> {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => {
    pageErrors.push(error.message);
  });
  return { consoleErrors, pageErrors };
}

async function runScenario(
  page: Page,
  locale: BaselineLocale,
  width: number,
  scenario: BaselineScenario,
) {
  const { unknownRequests } = await installRoutes(page, scenario, locale);
  const { consoleErrors, pageErrors } = await collectErrors(page);
  const httpErrors: Array<{ path: string; status: number }> = [];
  page.on("response", (response) => {
    if (response.status() >= 400) {
      httpErrors.push({
        path: new URL(response.url()).pathname,
        status: response.status(),
      });
    }
  });
  const visibleCheckpoints: Record<string, boolean> = {};

  await page.goto("/");
  await chooseLoginLocale(page, locale);
  const loginHeading = page.getByRole("heading", { name: headingFor(locale, scenario) });
  await expect(loginHeading).toBeVisible();
  visibleCheckpoints.login = await loginHeading.isVisible();

  if (scenario === "account-read") {
    await page.getByTestId("account-username-input").fill("reader-user");
    await page.getByTestId("account-password-input").fill("baseline-password");
    await page.getByTestId("account-login-submit").click();
  } else {
    await page.getByTestId("admin-token-input").fill("baseline-token");
    await page.getByTestId("admin-token-submit").click();
  }

  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
  visibleCheckpoints.adapter_catalog = await page.getByTestId("adapter-catalog").isVisible();
  await expect(page.getByTestId("system-status-summary")).toHaveText(
    locale === "zh-CN" ? "系统正常" : "System normal",
  );
  visibleCheckpoints.system_status = await page.getByTestId("system-status-summary").isVisible();

  if (scenario === "account-read") {
    await expect(page.getByTestId("account-principal")).toContainText("reader-user");
    visibleCheckpoints.account_principal = await page.getByTestId("account-principal").isVisible();
  }

  await page.getByTestId("adapter-item").first().click();
  await expect(page.getByTestId("workbench-header")).toBeVisible();
  await expect(page.getByTestId("editor-main")).toBeVisible();
  visibleCheckpoints.workbench_header = await page.getByTestId("workbench-header").isVisible();
  visibleCheckpoints.editor_main = await page.getByTestId("editor-main").isVisible();

  if (scenario === "account-read") {
    await expect(page.getByTestId("adapter-read-only")).toBeVisible();
    visibleCheckpoints.read_only_permission = await page.getByTestId("adapter-read-only").isVisible();
  } else {
    await expect(page.getByTestId("header-task-run-once")).toBeVisible();
    visibleCheckpoints.admin_runtime_action = await page.getByTestId("header-task-run-once").isVisible();
  }

  await page.waitForTimeout(250);
  const overflow = await page.evaluate(() => ({
    inner_width: window.innerWidth,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
  }));
  const screenshotName = `${locale}-${width}-${scenario}.png`;
  const screenshotPath = resolve(baselineOutputDir, screenshotName);
  mkdirSync(baselineOutputDir, { recursive: true });
  await page.screenshot({ path: screenshotPath, fullPage: true, animations: "disabled" });

  const expectedHttpErrors = httpErrors.filter(
    (error) => scenario === "account-read" && error.path === "/api/auth/account/me" && error.status === 401,
  );
  const unexpectedHttpErrors = httpErrors.filter((error) => !expectedHttpErrors.includes(error));
  const expectedConsoleMessage = "Failed to load resource: the server responded with a status of 401 (Unauthorized)";
  const expectedConsoleErrors = consoleErrors.filter((message) => message === expectedConsoleMessage);
  const unexpectedConsoleErrors = consoleErrors.filter((message) => message !== expectedConsoleMessage);

  expect(unknownRequests).toEqual([]);
  expect(expectedConsoleErrors).toHaveLength(expectedHttpErrors.length);
  expect(unexpectedHttpErrors).toEqual([]);
  expect(unexpectedConsoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
  expect(overflow.document_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(overflow.body_scroll_width).toBeLessThanOrEqual(overflow.inner_width);

  records.push({
    locale,
    width,
    scenario,
    screenshot: `docs/ui/m5-10-wave-a/baseline/${screenshotName}`,
    console_errors: consoleErrors,
    expected_console_errors: expectedConsoleErrors,
    unexpected_console_errors: unexpectedConsoleErrors,
    page_errors: pageErrors,
    http_errors: httpErrors,
    unexpected_http_errors: unexpectedHttpErrors,
    unknown_requests: unknownRequests,
    overflow,
    visible_checkpoints: visibleCheckpoints,
  });
}

test.afterAll(() => {
  mkdirSync(baselineOutputDir, { recursive: true });
  records.sort((left, right) =>
    left.locale.localeCompare(right.locale) || left.width - right.width || left.scenario.localeCompare(right.scenario),
  );
  writeFileSync(
    resolve(baselineOutputDir, "baseline-report.json"),
    `${JSON.stringify({
      schema_version: 1,
      product: "DataLinkRuntime",
      antd: "5.29.3",
      pro_components: "2.8.10",
      playwright: "1.62.1",
      browser: "chromium",
      browser_version: browserVersion,
      node_version: process.version,
      platform: process.platform,
      color_scheme: "light",
      viewport_widths: VIEWPORTS,
      viewport_height: 900,
      locales: LOCALES,
      scenarios: ["token-login-workbench", "account-login-read-permission"],
      records,
    }, null, 2)}\n`,
    "utf8",
  );
});

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`${locale} ${width}px baseline`, async ({ browser }) => {
      browserVersion = browser.version();
      const context = await browser.newContext({
        locale,
        viewport: { width, height: 900 },
      });
      const tokenPage = await context.newPage();
      await runScenario(tokenPage, locale, width, "token");
      await tokenPage.close();

      const accountPage = await context.newPage();
      await runScenario(accountPage, locale, width, "account-read");
      await accountPage.close();
      await context.close();
    });
  }
}
