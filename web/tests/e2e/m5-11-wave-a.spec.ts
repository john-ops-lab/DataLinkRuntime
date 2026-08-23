import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page, type Route } from "@playwright/test";

type Locale = "zh-CN" | "en";
type AccountPersona = "force-password" | "read";

const LOCALES: readonly Locale[] = ["zh-CN", "en"];
const VIEWPORTS = [1280, 1440, 1680, 1920] as const;
const specDir = dirname(fileURLToPath(import.meta.url));
const evidenceRoot = resolve(
  specDir,
  process.env.DLR_WAVE_A_OUTPUT_DIR ?? "../../../docs/ui/m5-11-wave-a",
);
const screenshotDir = resolve(evidenceRoot, "browser");

const worker = {
  id: 1,
  name: "fixture-worker-with-a-long-display-name",
  status: "online",
  last_heartbeat: "2026-01-01T00:00:00Z",
  capabilities: ["python", "javascript", "java"],
};

const adapters = Array.from({ length: 24 }, (_, index) => ({
  id: index + 1,
  name: index === 0
    ? "订单同步适配器 Orders Synchronization Adapter — Long Label"
    : `fixture-adapter-${String(index + 1).padStart(2, "0")}`,
  description: "Wave A browser fixture with long Chinese and English copy.",
  language: "python",
  adapter_type: "task",
  run_mode: "manual",
  timeout_seconds: 300,
  owner_user_id: 42,
  owner_username: "fixture-owner",
  latest_version_id: index === 0 ? 10 : null,
  runtime_worker_id: 1,
  runtime_locked: false,
  archived_at: null,
  running_execution_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  access_level: "admin",
}));

const version = {
  id: 10,
  adapter_id: 1,
  seq: 1,
  created_at: "2026-01-01T00:00:00Z",
  code: "def handle(context, input):\n    return {\"fixture\": True, \"input\": input}\n",
  requirements: "requests==2.32.3\n",
  runtime_config: { fixture: true },
};

const knowledgeSource = {
  source_id: "ima",
  kind: "ima",
  name: "Fixture Knowledge Source",
  endpoint: "https://knowledge.example.invalid",
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
  scenario: string;
  locale: Locale;
  width: number;
  height: number;
  zoom: string;
  screenshots: string[];
  overflow: {
    innerWidth: number;
    documentScrollWidth: number;
    bodyScrollWidth: number;
    settingsScrollBoundary: boolean;
    catalogScrollBoundary: boolean;
  };
  consoleErrors: string[];
  expectedConsoleErrors: string[];
  pageErrors: string[];
  unknownRequests: string[];
}

const records: BrowserRecord[] = [];

function jsonBody(body: unknown): string {
  return JSON.stringify(body);
}

function errorBody(code: string, message: string) {
  return { detail: { code, message } };
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: jsonBody(body),
  });
}

async function installRoutes(
  page: Page,
  mode: "token" | "account",
  locale: Locale,
  accountPersona?: AccountPersona,
): Promise<{ unknownRequests: string[]; accountLoggedIn: { value: boolean } }> {
  const unknownRequests: string[] = [];
  const accountLoggedIn = { value: false };

  await page.route("**/entry-mode.js", async (route) => {
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
    if (path === "/api/locale" && method === "PUT") {
      const payload = request.postDataJSON() as { locale?: Locale };
      await fulfillJson(route, { locale: payload.locale === "en" ? "en" : "zh-CN" });
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
      await fulfillJson(route, mode === "account"
        ? adapters.map((adapter) => ({ ...adapter, access_level: "read" }))
        : adapters);
      return;
    }
    if (path === "/api/adapters/1/versions" && method === "GET") {
      await fulfillJson(route, [{ id: 10, adapter_id: 1, seq: 1, created_at: version.created_at }]);
      return;
    }
    if (path === "/api/adapters/1/versions/10" && method === "GET") {
      await fulfillJson(route, version);
      return;
    }
    if (path === "/api/adapters/1/schedule" && method === "GET") {
      await fulfillJson(route, errorBody("schedule_not_configured", "Fixture schedule is not configured"), 404);
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
    if (path === "/api/auth/admin/verify" && method === "GET" && mode === "token") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/auth/account/csrf" && method === "GET" && mode === "account") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/auth/account/me" && method === "GET" && mode === "account") {
      if (!accountLoggedIn.value) {
        await fulfillJson(route, errorBody("account_session_required", "Fixture account session required"), 401);
      } else {
        await fulfillJson(route, {
          principal: {
            id: 7,
            username: accountPersona === "read" ? "fixture-reader" : "fixture-admin",
            role: accountPersona === "read" ? "user" : "admin",
            enabled: true,
            must_change_password: accountPersona === "force-password",
          },
        });
      }
      return;
    }
    if (path === "/api/auth/account/login" && method === "POST" && mode === "account") {
      accountLoggedIn.value = true;
      await fulfillJson(route, {
        principal: {
          id: 7,
          username: accountPersona === "read" ? "fixture-reader" : "fixture-admin",
          role: accountPersona === "read" ? "user" : "admin",
          enabled: true,
          must_change_password: accountPersona === "force-password",
        },
      });
      return;
    }
    if (path === "/api/auth/account/change-password" && method === "POST" && mode === "account") {
      accountLoggedIn.value = false;
      await fulfillJson(route, { status: "ok" });
      return;
    }

    const credential = {
      id: 1,
      name: "fixture-token-credential-with-a-long-name",
      type: "token",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    };
    if (path === "/api/credentials" && method === "GET") {
      await fulfillJson(route, [credential]);
      return;
    }
    if (path === "/api/package-sources" && method === "GET") {
      await fulfillJson(route, []);
      return;
    }
    if (path === "/api/package-sources/defaults" && method === "GET") {
      await fulfillJson(route, {
        pypi: { kind: "pypi", name: "Fixture PyPI", index_url: "https://pypi.example.invalid/simple/" },
        npm: { kind: "npm", name: "Fixture npm", index_url: "https://npm.example.invalid/" },
        maven: { kind: "maven", name: "Fixture Maven", index_url: "https://maven.example.invalid/" },
      });
      return;
    }
    if (path === "/api/ai/settings" && method === "GET") {
      await fulfillJson(route, null);
      return;
    }
    if (path === "/api/ai/providers" && method === "GET") {
      await fulfillJson(route, { providers: [] });
      return;
    }
    if (path === "/api/ai/custom-providers" && method === "GET") {
      await fulfillJson(route, { providers: [] });
      return;
    }
    if (path === "/api/knowledge-sources/ima" && method === "GET") {
      await fulfillJson(route, knowledgeSource);
      return;
    }

    const requestKey = `${method} ${path}${url.search}`;
    unknownRequests.push(requestKey);
    await fulfillJson(route, errorBody("wave_a_unhandled_request", requestKey), 404);
  });

  return { unknownRequests, accountLoggedIn };
}

function headingFor(locale: Locale, mode: "token" | "account", persona?: AccountPersona): string {
  if (mode === "account") {
    if (persona === "force-password") {
      return locale === "zh-CN" ? "修改账号密码" : "Change your password";
    }
    return locale === "zh-CN" ? "账号登录" : "Account login";
  }
  return locale === "zh-CN" ? "欢迎登录 DLR 控制台" : "Welcome to the DLR Console";
}

async function chooseLoginLocale(page: Page, locale: Locale): Promise<void> {
  const select = page.getByTestId("login-locale-select");
  await select.click();
  const dropdown = page.locator(".ant-select-dropdown:visible");
  await expect(dropdown).toBeVisible();
  await dropdown.locator(".ant-select-item-option").filter({
    hasText: locale === "zh-CN" ? "简体中文" : "English",
  }).click();
}

async function loginToken(page: Page, locale: Locale): Promise<void> {
  await expect(page.getByRole("heading", { name: headingFor(locale, "token") })).toBeVisible();
  await expect(page.getByTestId("login-locale-select")).toBeVisible();
  await chooseLoginLocale(page, locale);
  await page.getByTestId("admin-token-input").fill("FAKE_ADMIN_TOKEN");
  await page.getByTestId("admin-token-submit").click();
  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
}

async function collectLayoutMetrics(page: Page) {
  return page.evaluate(() => {
    const catalogList = document.querySelector<HTMLElement>(".catalog-list");
    const settingsContent = document.querySelector<HTMLElement>(".settings-center-content");
    return {
      innerWidth: window.innerWidth,
      documentScrollWidth: document.documentElement.scrollWidth,
      bodyScrollWidth: document.body.scrollWidth,
      settingsScrollBoundary: settingsContent?.style.overflowY === "auto" || getComputedStyle(settingsContent ?? document.body).overflowY === "auto",
      catalogScrollBoundary: catalogList?.scrollHeight === undefined || catalogList.scrollHeight >= catalogList.clientHeight,
    };
  });
}

async function assertDiagnostics(
  page: Page,
  unknownRequests: string[],
  consoleErrors: string[],
  pageErrors: string[],
) {
  expect(unknownRequests).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
}

function isExpectedAccountBootstrapError(message: string): boolean {
  return message === "Failed to load resource: the server responded with a status of 401 (Unauthorized)";
}

async function recordScreenshot(page: Page, name: string): Promise<string> {
  mkdirSync(screenshotDir, { recursive: true });
  const filename = `${name}.png`;
  await page.screenshot({ path: resolve(screenshotDir, filename), fullPage: true, animations: "disabled" });
  return `docs/ui/m5-11-wave-a/browser/${filename}`;
}

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`Wave A admin shell/settings ${locale} ${width}px`, async ({ page }) => {
      const height = width === 1280 ? 720 : width === 1440 ? 800 : width === 1680 ? 900 : 1080;
      await page.setViewportSize({ width, height });
      const consoleErrors: string[] = [];
      const pageErrors: string[] = [];
      page.on("console", (message) => {
        if (message.type() === "error") consoleErrors.push(message.text());
      });
      page.on("pageerror", (error) => pageErrors.push(error.message));
      const { unknownRequests } = await installRoutes(page, "token", locale);

      await page.goto("/");
      await loginToken(page, locale);
      await expect(page.getByTestId("control-status")).toHaveText(
        locale === "zh-CN" ? "控制服务正常" : "Control service healthy",
      );
      await page.getByTestId("adapter-item").first().click();
      await expect(page.getByTestId("workbench-header")).toBeVisible();
      await expect(page.getByTestId("editor-main")).toBeVisible();

      const workspaceMetrics = await collectLayoutMetrics(page);
      expect(workspaceMetrics.documentScrollWidth).toBeLessThanOrEqual(workspaceMetrics.innerWidth);
      expect(workspaceMetrics.bodyScrollWidth).toBeLessThanOrEqual(workspaceMetrics.innerWidth);
      const screenshots = [await recordScreenshot(page, `${locale}-${width}-workspace`)];

      await page.getByTestId("user-menu").click();
      await page.getByRole("menuitem", { name: locale === "zh-CN" ? "系统设置" : "System Settings" }).click();
      await expect(page).toHaveURL(/\/settings\/general$/);
      await expect(page.getByTestId("system-settings-center")).toBeVisible();
      await expect(page.getByRole("heading", { level: 3, name: locale === "zh-CN" ? "常规" : "General" })).toBeVisible();
      const settingsMetrics = await collectLayoutMetrics(page);
      expect(settingsMetrics.documentScrollWidth).toBeLessThanOrEqual(settingsMetrics.innerWidth);
      expect(settingsMetrics.bodyScrollWidth).toBeLessThanOrEqual(settingsMetrics.innerWidth);
      expect(settingsMetrics.settingsScrollBoundary).toBe(true);
      screenshots.push(await recordScreenshot(page, `${locale}-${width}-settings-general`));

      await page.getByRole("menuitem", { name: locale === "zh-CN" ? "AI 模型" : "AI model" }).click();
      await expect(page.getByTestId("ai-model-settings-panel")).toBeVisible();
      await page.getByTestId("ai-base-url").fill("https://models.example.invalid/v1");
      await page.getByTestId("ai-model-input").fill("fixture-model");
      let dismissed = false;
      page.once("dialog", async (dialog) => {
        dismissed = true;
        await dialog.dismiss();
      });
      await page.getByRole("menuitem", { name: locale === "zh-CN" ? "凭据" : "Credentials" }).click();
      expect(dismissed).toBe(true);
      await expect(page).toHaveURL(/\/settings\/ai-model$/);

      let accepted = false;
      page.once("dialog", async (dialog) => {
        accepted = true;
        await dialog.accept();
      });
      await page.getByTestId("settings-back").click();
      expect(accepted).toBe(true);
      await expect(page.getByTestId("adapter-catalog")).toBeVisible();
      expect(page.url()).not.toMatch(/\/settings\//);
      screenshots.push(await recordScreenshot(page, `${locale}-${width}-back-to-workspace`));

      await assertDiagnostics(page, unknownRequests, consoleErrors, pageErrors);
      records.push({
        scenario: "token-admin",
        locale,
        width,
        height,
        zoom: "100%",
        screenshots,
        overflow: settingsMetrics,
        consoleErrors,
        expectedConsoleErrors: [],
        pageErrors,
        unknownRequests,
      });
    });
  }
}

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`Wave V1 credentials pilot ${locale} ${width}px`, async ({ page }) => {
      const height = width === 1280 ? 720 : width === 1440 ? 800 : width === 1680 ? 900 : 1080;
      await page.setViewportSize({ width, height });
      const consoleErrors: string[] = [];
      const pageErrors: string[] = [];
      page.on("console", (message) => {
        if (message.type() === "error") consoleErrors.push(message.text());
      });
      page.on("pageerror", (error) => pageErrors.push(error.message));
      const { unknownRequests } = await installRoutes(page, "token", locale);

      await page.goto("/");
      await loginToken(page, locale);
      await page.getByTestId("user-menu").click();
      await page.getByRole("menuitem", { name: locale === "zh-CN" ? "系统设置" : "System Settings" }).click();
      await page.getByRole("menuitem", { name: locale === "zh-CN" ? "凭据" : "Credentials" }).click();

      await expect(page.getByTestId("credentials-panel")).toBeVisible();
      await expect(page.getByRole("heading", { level: 3, name: locale === "zh-CN" ? "凭据" : "Credentials" })).toBeVisible();
      await expect(page.getByTestId("credentials-toolbar")).toHaveRole("toolbar");
      await expect(page.getByRole("textbox", { name: locale === "zh-CN" ? "筛选凭据" : "Filter credentials" })).toBeVisible();
      await expect(page.getByRole("combobox", { name: locale === "zh-CN" ? "筛选凭据类型" : "Filter credential type" })).toBeVisible();
      await expect(page.getByTestId("new-credential")).toBeVisible();
      await expect(page.getByTestId("refresh-credentials")).toBeVisible();
      await expect(page.getByTestId("credential-row")).toContainText("fixture-token-credential-with-a-long-name");
      await expect(page.getByTestId("credential-row")).toHaveAttribute(
        "title",
        "fixture-token-credential-with-a-long-name",
      );
      await expect(page.getByTestId("update-credential")).toHaveAttribute(
        "aria-label",
        locale === "zh-CN"
          ? "编辑凭据 fixture-token-credential-with-a-long-name"
          : "Edit credential fixture-token-credential-with-a-long-name",
      );
      await expect(page.getByTestId("delete-credential")).toHaveAttribute(
        "aria-label",
        locale === "zh-CN"
          ? "删除凭据 fixture-token-credential-with-a-long-name"
          : "Delete credential fixture-token-credential-with-a-long-name",
      );

      await page.getByTestId("credential-help").click();
      await expect(page.getByTestId("credential-type-guide")).toBeVisible();
      await expect(page.getByTestId("credential-type-guide")).toContainText(
        locale === "zh-CN" ? "不同凭据类型" : "Credential types are templates",
      );

      const layout = await page.evaluate(() => {
        const toolbar = document.querySelector<HTMLElement>("[data-testid=credentials-toolbar]");
        const filters = document.querySelector<HTMLElement>("[data-testid=credentials-filters]");
        const actions = toolbar?.querySelector<HTMLElement>(".settings-toolbar-actions");
        const table = document.querySelector<HTMLElement>(".credentials-table .ant-table-container");
        return {
          innerWidth: window.innerWidth,
          documentScrollWidth: document.documentElement.scrollWidth,
          bodyScrollWidth: document.body.scrollWidth,
          toolbarSingleRow: toolbar !== null && filters !== null && actions !== null
            ? Math.abs(filters.getBoundingClientRect().top - actions.getBoundingClientRect().top) < 4
            : false,
          tableFits: table === null || table.scrollWidth <= table.clientWidth,
          documentLanguage: document.documentElement.lang,
        };
      });
      expect(layout.documentScrollWidth).toBeLessThanOrEqual(layout.innerWidth);
      expect(layout.bodyScrollWidth).toBeLessThanOrEqual(layout.innerWidth);
      expect(layout.toolbarSingleRow).toBe(true);
      expect(layout.tableFits).toBe(true);
      expect(layout.documentLanguage).toBe(locale);
      expect(page.locator("body")).not.toContainText("FAKE_ADMIN_TOKEN");

      const screenshots = [await recordScreenshot(page, `v1-credentials-${locale}-${width}`)];
      await assertDiagnostics(page, unknownRequests, consoleErrors, pageErrors);
      records.push({
        scenario: "v1-credentials-pilot",
        locale,
        width,
        height,
        zoom: "100%",
        screenshots,
        overflow: {
          innerWidth: layout.innerWidth,
          documentScrollWidth: layout.documentScrollWidth,
          bodyScrollWidth: layout.bodyScrollWidth,
          settingsScrollBoundary: true,
          catalogScrollBoundary: true,
        },
        consoleErrors,
        expectedConsoleErrors: [],
        pageErrors,
        unknownRequests,
      });
    });
  }
}

test("Wave A zoom and account forced-password shell", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  const { unknownRequests } = await installRoutes(page, "account", "zh-CN", "force-password");

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "账号登录" })).toBeVisible();
  await expect(page.getByTestId("login-locale-select")).toBeVisible();
  await chooseLoginLocale(page, "en");
  await expect(page.getByRole("heading", { name: "Account login" })).toBeVisible();
  await expect(page.evaluate(() => localStorage.getItem("dlr-login-locale"))).resolves.toBe("en");
  await page.getByTestId("account-username-input").fill("FAKE_ACCOUNT");
  await page.getByTestId("account-password-input").fill("FAKE_PASSWORD");
  await page.getByTestId("account-login-submit").click();
  await expect(page.getByRole("heading", { name: "Change your password" })).toBeVisible();
  await expect(page.getByTestId("login-locale-select")).toBeVisible();
  await expect(page.locator("body")).not.toContainText(/HttpOnly|account Session/i);

  await page.evaluate(() => {
    document.documentElement.style.zoom = "1.25";
  });
  const metrics = await collectLayoutMetrics(page);
  expect(metrics.documentScrollWidth).toBeLessThanOrEqual(metrics.innerWidth);
  expect(metrics.bodyScrollWidth).toBeLessThanOrEqual(metrics.innerWidth);
  const screenshots = [await recordScreenshot(page, "en-1280-zoom125-force-password")];
  const expectedConsoleErrors = consoleErrors.filter(isExpectedAccountBootstrapError);
  const unexpectedConsoleErrors = consoleErrors.filter((message) => !isExpectedAccountBootstrapError(message));
  await assertDiagnostics(page, unknownRequests, unexpectedConsoleErrors, pageErrors);
  records.push({
    scenario: "account-force-password",
    locale: "en",
    width: 1280,
    height: 720,
    zoom: "125%",
    screenshots,
    overflow: metrics,
    consoleErrors: unexpectedConsoleErrors,
    expectedConsoleErrors,
    pageErrors,
    unknownRequests,
  });
});

test("Wave A account permission and direct settings route", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  const { unknownRequests } = await installRoutes(page, "account", "en", "read");

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Account login" })).toBeVisible();
  await page.getByTestId("account-username-input").fill("FAKE_READER");
  await page.getByTestId("account-password-input").fill("FAKE_PASSWORD");
  await page.getByTestId("account-login-submit").click();
  await expect(page.getByTestId("account-principal")).toContainText("fixture-reader");
  await page.getByTestId("user-menu").click();
  await expect(page.getByRole("menuitem", { name: "Account profile" })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "System Settings" })).toHaveCount(0);
  await expect(page.getByRole("menuitem", { name: "User management" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  await page.goto("/settings/general");
  await expect(page).toHaveURL(/\/adapters$/);
  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
  const metrics = await collectLayoutMetrics(page);
  expect(metrics.documentScrollWidth).toBeLessThanOrEqual(metrics.innerWidth);
  expect(metrics.bodyScrollWidth).toBeLessThanOrEqual(metrics.innerWidth);
  const screenshots = [await recordScreenshot(page, "en-1440-account-read-adapters")];
  const expectedConsoleErrors = consoleErrors.filter(isExpectedAccountBootstrapError);
  const unexpectedConsoleErrors = consoleErrors.filter((message) => !isExpectedAccountBootstrapError(message));
  await assertDiagnostics(page, unknownRequests, unexpectedConsoleErrors, pageErrors);
  records.push({
    scenario: "account-read-permission",
    locale: "en",
    width: 1440,
    height: 900,
    zoom: "100%",
    screenshots,
    overflow: metrics,
    consoleErrors: unexpectedConsoleErrors,
    expectedConsoleErrors,
    pageErrors,
    unknownRequests,
  });
});

test.afterAll(() => {
  mkdirSync(evidenceRoot, { recursive: true });
  writeFileSync(
    resolve(evidenceRoot, "browser-report.json"),
    `${JSON.stringify({
      schema_version: 1,
      product: "DataLinkRuntime",
      wave: "M5.11 Wave A",
      antd: "5.29.3",
      pro_components: "2.8.10",
      playwright: "1.62.1",
      browser: "chromium",
      viewport_widths: VIEWPORTS,
      locales: LOCALES,
      records,
    }, null, 2)}\n`,
  );
});
