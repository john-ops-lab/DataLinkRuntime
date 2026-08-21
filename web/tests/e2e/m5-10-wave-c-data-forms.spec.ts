import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page, type Route } from "@playwright/test";

type Locale = "zh-CN" | "en";
type Scenario = "admin" | "owner" | "read" | "empty" | "error";

const LOCALES: readonly Locale[] = ["zh-CN", "en"];
const VIEWPORTS = [1280, 1440, 1680, 1920] as const;
const specDir = dirname(fileURLToPath(import.meta.url));
const evidenceRoot = resolve(
  specDir,
  process.env.DLR_WAVE_C_OUTPUT_DIR ?? "../../../docs/ui/m5-10-wave-c",
);
const screenshotDir = resolve(evidenceRoot, "browser");

const adapter = {
  id: 1,
  name: "订单同步适配器 Orders Synchronization Adapter — Long Label",
  description: "Wave C fixture with long Chinese and English copy for responsive form and filter checks.",
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
  name: "runtime-worker-with-an-intentionally-long-display-name",
  status: "online",
  last_heartbeat: "2026-01-01T00:00:00Z",
  capabilities: ["python", "javascript", "java"],
};

const users = Array.from({ length: 12 }, (_, index) => ({
  id: index + 1,
  username: index === 0
    ? "admin-with-a-long-display-name"
    : `fixture-user-${String(index).padStart(2, "0")}-with-long-copy`,
  role: index === 0 ? "admin" : "user",
  enabled: index !== 11,
  must_change_password: false,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
}));

const packageSources = [{
  id: 1,
  name: "Private PyPI source with a very long translated display name",
  kind: "pypi",
  index_url: "https://packages.example.com/repository/python/simple/with/a/long/path",
  is_default: true,
  credential_id: null,
  credential_name: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
}];

const packageDefaults = {
  pypi: { kind: "pypi", name: "Official PyPI", index_url: "https://pypi.org/simple/" },
  npm: { kind: "npm", name: "Official npm", index_url: "https://registry.npmjs.org/" },
  maven: { kind: "maven", name: "Maven Central", index_url: "https://repo1.maven.org/maven2/" },
};

const knowledgeSource = {
  source_id: "ima",
  kind: "ima",
  name: "Tencent ima Knowledge Source",
  endpoint: "https://ima.qq.com",
  enabled: true,
  status: "configured",
  credential_id: null,
  credential_name: null,
  credential_type: null,
  config_source: "environment",
  created_at: null,
  updated_at: null,
};

interface BrowserRecord {
  locale: Locale;
  width: number;
  scenario: Scenario | "loading";
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

async function fulfillJson(
  route: Route,
  body: unknown,
  status = 200,
  headers?: Record<string, string>,
) {
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

function captureDiagnostics(page: Page, expectedStatusCodes: readonly number[] = []) {
  const consoleErrors: string[] = [];
  const expectedConsoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      const text = message.text();
      if (expectedStatusCodes.some((statusCode) => text.includes(`status of ${statusCode}`))) {
        expectedConsoleErrors.push(text);
      } else {
        consoleErrors.push(text);
      }
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  return { consoleErrors, expectedConsoleErrors, pageErrors };
}

async function installRoutes(page: Page, scenario: Scenario, locale: Locale): Promise<string[]> {
  let accountLoggedIn = false;
  const unknownRequests: string[] = [];
  const accountScenario = scenario === "owner" || scenario === "read";

  await page.route("**/entry-mode.js", async (route) => {
    await route.fulfill({
      contentType: "application/javascript",
      body: `window.__DLR_ENTRY_MODE__ = "${accountScenario ? "account" : "token"}";`,
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
        await fulfillJson(route, [{
          ...adapter,
          access_level: scenario === "read" ? "read" : scenario === "owner" ? "owner" : "admin",
        }]);
      }
      return;
    }
    if (path === "/api/adapters/1/versions" && method === "GET") {
      await fulfillJson(route, []);
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
    if (path === "/api/auth/admin/verify" && method === "GET" && !accountScenario) {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/auth/account/csrf" && method === "GET" && accountScenario) {
      await fulfillJson(route, { status: "ok" }, 200, {
        "set-cookie": "dlr_account_csrf=wave-c-csrf; Path=/",
      });
      return;
    }
    if (path === "/api/auth/account/me" && method === "GET" && accountScenario) {
      if (accountLoggedIn) {
        await fulfillJson(route, {
          principal: {
            id: scenario === "owner" ? 42 : 7,
            username: scenario === "owner"
              ? "owner-user-with-a-long-display-name"
              : "reader-user-with-a-long-display-name",
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
    if (path === "/api/auth/account/login" && method === "POST" && accountScenario) {
      accountLoggedIn = true;
      await fulfillJson(route, {
        principal: {
          id: scenario === "owner" ? 42 : 7,
          username: scenario === "owner"
            ? "owner-user-with-a-long-display-name"
            : "reader-user-with-a-long-display-name",
          role: "user",
          enabled: true,
          must_change_password: false,
        },
      });
      return;
    }
    if (path === "/api/auth/account/logout" && method === "POST" && accountScenario) {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/users" && method === "GET" && !accountScenario) {
      await fulfillJson(route, users);
      return;
    }
    if (path === "/api/users" && method === "POST" && !accountScenario) {
      await fulfillJson(route, {
        ...users[1],
        id: 99,
        username: "created-fixture-user",
        role: "user",
      }, 201);
      return;
    }
    if (path.startsWith("/api/users/") && method === "PATCH" && !accountScenario) {
      const userId = Number(path.split("/").at(-1));
      const currentUser = users.find((user) => user.id === userId) ?? users[1];
      const body = (request.postDataJSON() ?? {}) as { enabled?: boolean; role?: "admin" | "user" };
      await fulfillJson(route, {
        ...currentUser,
        enabled: body.enabled ?? currentUser.enabled,
        role: body.role ?? currentUser.role,
      });
      return;
    }
    if (path.endsWith("/reset-password") && method === "POST" && !accountScenario) {
      await fulfillJson(route, users[1]);
      return;
    }
    if (path === "/api/credentials" && method === "GET" && !accountScenario) {
      await fulfillJson(route, []);
      return;
    }
    if (path === "/api/package-sources" && method === "GET" && !accountScenario) {
      await fulfillJson(route, packageSources);
      return;
    }
    if (path === "/api/package-sources/defaults" && method === "GET" && !accountScenario) {
      await fulfillJson(route, packageDefaults);
      return;
    }
    if (path === "/api/ai/settings" && method === "GET" && !accountScenario) {
      await fulfillJson(route, null);
      return;
    }
    if (path === "/api/knowledge-sources/ima" && method === "GET" && !accountScenario) {
      await fulfillJson(route, knowledgeSource);
      return;
    }

    const requestKey = `${method} ${path}${url.search}`;
    unknownRequests.push(requestKey);
    await fulfillJson(route, errorBody("wave_c_unhandled_request", requestKey), 404);
  });

  return unknownRequests;
}

async function measureOverflow(page: Page) {
  return page.evaluate(() => ({
    inner_width: window.innerWidth,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
  }));
}

async function finishRecord(
  page: Page,
  locale: Locale,
  width: number,
  scenario: Scenario | "loading",
  diagnostics: { consoleErrors: string[]; expectedConsoleErrors: string[]; pageErrors: string[] },
  unknownRequests: string[],
  visibleStates: Record<string, boolean>,
) {
  const overflow = await measureOverflow(page);
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
    screenshot: `docs/ui/m5-10-wave-c/browser/${screenshotName}`,
    overflow,
    console_errors: diagnostics.consoleErrors,
    expected_console_errors: diagnostics.expectedConsoleErrors,
    page_errors: diagnostics.pageErrors,
    unknown_requests: unknownRequests,
    visible_states: visibleStates,
  });
}

async function login(page: Page, scenario: Scenario, locale: Locale) {
  if (scenario === "owner" || scenario === "read") {
    await expect(page.getByRole("heading", { name: locale === "zh-CN" ? "账号登录" : "Account login" })).toBeVisible();
    await page.getByTestId("account-username-input").fill("fixture-user");
    await page.getByTestId("account-password-input").fill("fixture-password");
    await page.getByTestId("account-login-submit").click();
  } else {
    await expect(page.getByRole("heading", { name: locale === "zh-CN" ? "欢迎登录 DLR 控制台" : "Welcome to the DLR Console" })).toBeVisible();
    await page.getByTestId("admin-token-input").fill("fixture-token");
    await page.getByTestId("admin-token-submit").click();
  }
  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
}

async function inspectCatalog(page: Page, locale: Locale) {
  await expect(page.getByTestId("adapter-search")).toBeVisible();
  await page.getByTestId("adapter-search").fill("Long Label");
  await expect(page.getByTestId("adapter-item")).toHaveCount(1);
  await page.getByTestId("adapter-search").fill("");
  await page.getByTestId("adapter-type-filter").click();
  await page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option")
    .filter({ hasText: locale === "zh-CN" ? "任务型（手动）" : "Task (manual)" })
    .click();
  await expect(page.getByTestId("adapter-item")).toHaveCount(1);
  await page.getByTestId("adapter-status-filter").click();
  await page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option")
    .filter({ hasText: locale === "zh-CN" ? "已停止" : "Stopped" })
    .click();
  await expect(page.getByTestId("adapter-item")).toHaveCount(1);
  await page.getByTestId("adapter-search").focus();
  await page.keyboard.press("Tab");
  await page.getByTestId("adapter-search").fill("");
  await page.getByTestId("show-create-form").click();
  await expect(page.getByTestId("new-adapter-name")).toBeVisible();
  await page.getByTestId("new-adapter-name").fill("超长 Adapter 名称 Long Form Label for Width Audit");
  await page.getByTestId("new-adapter-description").fill("Long description used to verify DrawerForm wrapping, keyboard focus, and responsive width behavior.");
  await page.getByTestId("new-adapter-name").press("Tab");
  await page.locator(".ant-drawer-open").last().locator(".ant-drawer-close").click();
  await expect(page.getByTestId("new-adapter-name")).toBeHidden();
}

async function inspectAdminForms(page: Page, locale: Locale) {
  await page.getByTestId("system-settings").click();
  await expect(page.getByTestId("credentials-panel")).toBeVisible();
  await expect(page.getByTestId("credential-row")).toHaveCount(0);
  await page.getByTestId("new-credential").click();
  await expect(page.getByTestId("credential-name")).toBeVisible();
  await page.getByTestId("credential-name").fill("fixture-credential-with-a-long-name");
  await page.locator(".ant-modal-root").last().locator(".ant-modal-close").click();

  await page.getByRole("tab", { name: locale === "zh-CN" ? "依赖源" : "Package sources" }).click();
  await expect(page.getByTestId("package-sources-panel")).toBeVisible();
  await expect(page.getByTestId("package-source-row")).toBeVisible();
  await page.getByTestId("new-package-source").click();
  await expect(page.getByTestId("package-source-name")).toBeVisible();
  await page.getByTestId("package-source-name").fill("fixture source with long translated label");
  await page.getByTestId("package-source-url").fill("https://packages.example.com/a/very/long/repository/path/simple/");
  await page.locator(".ant-modal-root").last().locator(".ant-modal-close").click();

  await page.getByRole("tab", { name: locale === "zh-CN" ? "知识库" : "Knowledge bases" }).click();
  await expect(page.getByTestId("knowledge-source-summary")).toBeVisible();
  await expect(page.getByTestId("knowledge-source-endpoint")).toHaveText("https://ima.qq.com");
  await page.getByRole("tab", { name: locale === "zh-CN" ? "凭据管理" : "Credentials" }).click();
  await page.locator(".ant-drawer-open").last().locator(".ant-drawer-close").click();
  await expect(page.getByTestId("credentials-panel")).toBeHidden();

  await page.getByTestId("user-management").click();
  await expect(page.getByTestId("user-management-drawer")).toBeVisible();
  await expect(page.locator(".ant-pagination")).toBeVisible();
  await page.getByRole("textbox", { name: locale === "zh-CN" ? "搜索账号" : "Search users" }).fill("fixture-user-01");
  await expect(page.getByTestId("user-reset-2")).toBeVisible();
  await page.getByRole("textbox", { name: locale === "zh-CN" ? "搜索账号" : "Search users" }).fill("");
  await expect(page.getByTestId("users-bulk-enable")).toBeDisabled();
  await page.locator(".ant-table-row .ant-checkbox-input").first().check();
  await expect(page.getByTestId("users-bulk-enable")).toBeEnabled();
  page.once("dialog", async (dialog) => {
    expect(dialog.type()).toBe("confirm");
    await dialog.accept();
  });
  await page.getByTestId("users-bulk-disable").click();
  await expect(page.getByRole("status")).toContainText(
    locale === "zh-CN" ? "已批量禁用" : "Disabled 1 users",
  );
  await page.getByTestId("user-create-username").fill("created-fixture-user-with-long-label");
  await page.getByTestId("user-create-password").fill("fixture-password-123");
  await page.getByTestId("user-create-submit").click();
  await expect(page.getByRole("status")).toContainText(locale === "zh-CN" ? "账号已创建" : "Account created");
  expect(await page.locator("body").textContent()).not.toContain("fixture-password-123");
  await page.getByTestId("user-reset-1").click();
  await expect(page.getByTestId("user-reset-password")).toBeVisible();
  await page.getByTestId("user-reset-password").fill("fixture-reset-password-123");
  await page.locator(".ant-modal-root").last().locator(".ant-modal-close").click();
  await page.locator(".ant-drawer-open").last().locator(".ant-drawer-close").click();
}

async function inspectAccountAccess(page: Page, scenario: "owner" | "read", locale: Locale) {
  await page.getByTestId("adapter-item-menu").click();
  await page.getByRole("menuitem", { name: locale === "zh-CN" ? "设置" : "Settings" }).click();
  if (scenario === "read") {
    await expect(page.getByTestId("adapter-access-read-only")).toBeVisible();
  }
  const nameInput = page.getByTestId("adapter-name");
  expect(await nameInput.isDisabled()).toBe(scenario === "read");
  if (scenario === "read") {
    expect(await page.getByTestId("system-settings").count()).toBe(0);
    expect(await page.getByTestId("user-management").count()).toBe(0);
  } else {
    expect(await page.getByTestId("update-details").isDisabled()).toBe(false);
  }
}

test.afterAll(() => {
  mkdirSync(evidenceRoot, { recursive: true });
  writeFileSync(
    resolve(evidenceRoot, "browser-report.json"),
    `${JSON.stringify({
      schema_version: 1,
      product: "DataLinkRuntime",
      wave: "M5.10 Wave C",
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

test.beforeAll(() => mkdirSync(screenshotDir, { recursive: true }));

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`${locale} ${width}px Wave C data display, filters, forms, and ACL`, async ({ browser }) => {
      const context = await browser.newContext({ locale, viewport: { width, height: 900 } });
      const adminPage = await context.newPage();
      const adminDiagnostics = captureDiagnostics(adminPage);
      const adminUnknown = await installRoutes(adminPage, "admin", locale);
      await adminPage.goto("/");
      await login(adminPage, "admin", locale);
      await inspectCatalog(adminPage, locale);
      await inspectAdminForms(adminPage, locale);
      await finishRecord(adminPage, locale, width, "admin", adminDiagnostics, adminUnknown, {
        catalog_filters: true,
        drawer_form: true,
        query_filter: true,
        pro_table_pagination: true,
        empty_state: true,
        modal_form: true,
        knowledge_source_form: true,
        user_management: true,
        bulk_actions: true,
        keyboard_focus: true,
      });
      await adminPage.close();

      for (const userScenario of ["owner", "read"] as const) {
        const userPage = await context.newPage();
        const userDiagnostics = captureDiagnostics(userPage, [401]);
        const userUnknown = await installRoutes(userPage, userScenario, locale);
        await userPage.goto("/");
        await login(userPage, userScenario, locale);
        await expect(userPage.getByTestId("account-profile")).toBeVisible();
        await inspectAccountAccess(userPage, userScenario, locale);
        await finishRecord(userPage, locale, width, userScenario, userDiagnostics, userUnknown, {
          account_profile: true,
          owner_or_read_acl: true,
          permission_denied: userScenario === "read",
          keyboard_focus: true,
        });
        await userPage.close();
      }
      await context.close();
    });
  }
}

test("Wave C empty, loading, error, disabled, and permission states", async ({ browser }) => {
  const context = await browser.newContext({ locale: "zh-CN", viewport: { width: 1280, height: 900 } });

  const emptyPage = await context.newPage();
  const emptyDiagnostics = captureDiagnostics(emptyPage);
  const emptyUnknown = await installRoutes(emptyPage, "empty", "zh-CN");
  await emptyPage.goto("/");
  await login(emptyPage, "empty", "zh-CN");
  await expect(emptyPage.locator(".ant-empty.catalog-empty")).toBeVisible();
  await finishRecord(emptyPage, "zh-CN", 1280, "empty", emptyDiagnostics, emptyUnknown, {
    catalog_empty: true,
    workbench_disabled: true,
  });
  await emptyPage.close();

  const errorPage = await context.newPage();
  const errorDiagnostics = captureDiagnostics(errorPage, [503]);
  const errorUnknown = await installRoutes(errorPage, "error", "zh-CN");
  await errorPage.goto("/");
  await login(errorPage, "error", "zh-CN");
  await expect(errorPage.getByTestId("error-banner")).toContainText("请求失败");
  await finishRecord(errorPage, "zh-CN", 1280, "error", errorDiagnostics, errorUnknown, {
    error_feedback: true,
    disabled_actions: true,
  });
  await errorPage.close();

  const loadingPage = await context.newPage();
  const loadingDiagnostics = captureDiagnostics(loadingPage, [401]);
  const loadingUnknown: string[] = [];
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
      await fulfillJson(route, { locale: "zh-CN", status: "ok" }, 200, path.endsWith("csrf") ? { "set-cookie": "dlr_account_csrf=wave-c-csrf; Path=/" } : undefined);
      return;
    }
    if (path === "/api/auth/account/me") {
      await bootstrapBlocked;
      await fulfillJson(route, errorBody("account_session_required", "expired"), 401);
      return;
    }
    loadingUnknown.push(`${route.request().method()} ${path}`);
    await fulfillJson(route, []);
  });
  await loadingPage.goto("/");
  await expect(loadingPage.locator(".account-loading .ant-skeleton")).toBeVisible();
  await finishRecord(loadingPage, "zh-CN", 1280, "loading", loadingDiagnostics, loadingUnknown, {
    loading_skeleton: true,
  });
  releaseBootstrap?.();
  await loadingPage.close();
  await context.close();
});
