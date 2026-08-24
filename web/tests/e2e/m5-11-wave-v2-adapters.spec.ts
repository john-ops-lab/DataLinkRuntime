import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page, type Route } from "@playwright/test";

type Locale = "zh-CN" | "en";

const LOCALES: readonly Locale[] = ["zh-CN", "en"];
const VIEWPORTS = [1280, 1440, 1680, 1920] as const;
const specDir = dirname(fileURLToPath(import.meta.url));
const evidenceRoot = resolve(
  specDir,
  process.env.DLR_WAVE_V2_OUTPUT_DIR ?? "../../../docs/ui/m5-11-wave-v2-adapters",
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
  description: "V2 fixture with long Chinese and English copy for responsive checks.",
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
}));

const version = {
  id: 10,
  adapter_id: 1,
  seq: 1,
  created_at: "2026-01-01T00:00:00Z",
  code: "def handle(context, input):\n    return {\"fixture\": True}\n",
  requirements: "",
  runtime_config: { fixture: true },
};

interface BrowserRecord {
  locale: Locale;
  width: number;
  screenshots: string[];
  overflow: {
    innerWidth: number;
    documentScrollWidth: number;
    bodyScrollWidth: number;
    catalogWidth: number;
    settingsWidth: number;
    catalogListWidth: number;
    settingsBodyWidth: number;
  };
  consoleErrors: string[];
  pageErrors: string[];
  unknownRequests: string[];
  language: string;
}

const records: BrowserRecord[] = [];

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

async function installRoutes(page: Page, locale: Locale): Promise<{
  unknownRequests: string[];
  adapterListCalls: { value: number };
}> {
  const unknownRequests: string[] = [];
  const adapterListCalls = { value: 0 };

  await page.route("**/entry-mode.js", async (route) => {
    await route.fulfill({
      contentType: "application/javascript",
      body: 'window.__DLR_ENTRY_MODE__ = "token";',
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
      adapterListCalls.value += 1;
      await fulfillJson(route, adapters);
      return;
    }
    if (path === "/api/auth/admin/verify" && method === "GET") {
      await fulfillJson(route, { status: "ok" });
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
    if (path === "/api/adapters/1/credential-bindings" && method === "GET") {
      await fulfillJson(route, []);
      return;
    }
    if (path === "/api/adapters/1/credential-options" && method === "GET") {
      await fulfillJson(route, []);
      return;
    }
    if (path === "/api/credentials" && method === "GET") {
      await fulfillJson(route, []);
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
      await fulfillJson(route, {
        source_id: "ima",
        kind: "ima",
        name: "Fixture Knowledge Source",
        endpoint: "https://knowledge.example.invalid",
        enabled: false,
        status: "unconfigured",
        credential_id: null,
        credential_name: null,
        credential_type: null,
        config_source: "environment",
        created_at: null,
        updated_at: null,
      });
      return;
    }

    const requestKey = `${method} ${path}${url.search}`;
    unknownRequests.push(requestKey);
    await fulfillJson(route, errorBody("v2_unhandled_request", requestKey), 404);
  });

  return { unknownRequests, adapterListCalls };
}

async function login(page: Page, locale: Locale): Promise<void> {
  await expect(page.getByRole("heading", {
    name: locale === "zh-CN" ? "欢迎登录 DLR 控制台" : "Welcome to the DLR Console",
  })).toBeVisible();
  await page.getByTestId("admin-token-input").fill("FAKE_ADMIN_TOKEN");
  await page.getByTestId("admin-token-submit").click();
  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
}

async function captureScreenshot(page: Page, name: string): Promise<string> {
  mkdirSync(screenshotDir, { recursive: true });
  const filename = `${name}.png`;
  await page.screenshot({ path: resolve(screenshotDir, filename), fullPage: true, animations: "disabled" });
  return `docs/ui/m5-11-wave-v2-adapters/browser/${filename}`;
}

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`Wave V2 Adapter catalog and settings ${locale} ${width}px`, async ({ page }) => {
      const height = width === 1280 ? 720 : width === 1440 ? 800 : width === 1680 ? 900 : 1080;
      await page.setViewportSize({ width, height });
      const consoleErrors: string[] = [];
      const pageErrors: string[] = [];
      page.on("console", (message) => {
        if (message.type() === "error") {
          consoleErrors.push(message.text());
        }
      });
      page.on("pageerror", (error) => pageErrors.push(error.message));
      const { unknownRequests, adapterListCalls } = await installRoutes(page, locale);

      await page.goto("/");
      await login(page, locale);

      const labels = locale === "zh-CN"
        ? {
            catalog: "适配器",
            toolbar: "适配器工具栏",
            search: "搜索适配器",
            type: "适配器类型筛选",
            status: "适配器状态筛选",
            refresh: "刷新适配器列表",
            help: "适配器目录帮助",
            helpText: "搜索名称或描述；类型和状态筛选可以叠加使用。",
            settings: "设置",
            settingsTitle: "适配器设置",
            saveChanges: "保存更改",
          }
        : {
            catalog: "Adapters",
            toolbar: "Adapter toolbar",
            search: "Search Adapters",
            type: "Adapter type filter",
            status: "Adapter status filter",
            refresh: "Refresh Adapter list",
            help: "Adapter catalog help",
            helpText: "Search by name or description; type and status filters can be combined.",
            settings: "Settings",
            settingsTitle: "Adapter settings",
            saveChanges: "Save changes",
          };

      const catalogHeader = page.getByTestId("adapter-catalog-header");
      await expect(catalogHeader.getByRole("heading", { name: labels.catalog })).toBeVisible();
      await expect(page.getByTestId("adapter-catalog-description")).toHaveCount(0);
      const toolbar = page.getByRole("toolbar", { name: labels.toolbar });
      await expect(toolbar.getByRole("textbox", { name: labels.search })).toBeVisible();
      await expect(toolbar.getByRole("combobox", { name: labels.type })).toBeVisible();
      await expect(toolbar.getByRole("combobox", { name: labels.status })).toBeVisible();
      await expect(page.getByTestId("show-create-form")).toBeVisible();
      await expect(page.getByTestId("refresh-adapters")).toHaveAttribute("aria-label", labels.refresh);
      await expect(page.getByTestId("adapter-catalog-help")).toHaveAttribute("aria-label", labels.help);
      await expect(page.getByTestId("adapter-catalog-summary")).toContainText("24");

      await page.getByTestId("adapter-catalog-help").click();
      await expect(page.getByText(labels.helpText)).toBeVisible();
      await page.keyboard.press("Escape");

      await page.getByTestId("refresh-adapters").focus();
      await page.keyboard.press("Enter");
      await expect.poll(() => adapterListCalls.value).toBeGreaterThanOrEqual(2);

      const catalogMetrics = await page.evaluate(() => {
        const catalog = document.querySelector<HTMLElement>("[data-testid=adapter-catalog]");
        const list = document.querySelector<HTMLElement>("[data-testid=adapter-catalog-list]");
        return {
          innerWidth: window.innerWidth,
          documentScrollWidth: document.documentElement.scrollWidth,
          bodyScrollWidth: document.body.scrollWidth,
          catalogWidth: catalog?.getBoundingClientRect().width ?? 0,
          catalogListWidth: list?.getBoundingClientRect().width ?? 0,
        };
      });
      expect(catalogMetrics.documentScrollWidth).toBeLessThanOrEqual(catalogMetrics.innerWidth);
      expect(catalogMetrics.bodyScrollWidth).toBeLessThanOrEqual(catalogMetrics.innerWidth);
      expect(catalogMetrics.catalogListWidth).toBeLessThanOrEqual(catalogMetrics.catalogWidth);
      const catalogScreenshot = await captureScreenshot(page, `catalog-${locale}-${width}`);

      await page.getByTestId("adapter-item").first().click();
      await expect(page.getByTestId("workbench-header")).toBeVisible();
      await page.getByTestId("adapter-item-menu").first().click();
      await page.getByRole("menuitem", { name: labels.settings }).click();
      await expect(page.getByTestId("adapter-settings-title")).toHaveText(labels.settingsTitle);
      await expect(page.getByTestId("adapter-settings-summary")).toContainText(adapters[0].name);
      await expect(page.getByTestId("adapter-settings-summary")).toContainText("Python");
      await expect(page.getByTestId("adapter-language")).toHaveAttribute("aria-readonly", "true");
      await expect(page.locator("input[data-testid=adapter-language]")).toHaveCount(0);
      await expect(page.getByTestId("adapter-danger-zone")).toBeVisible();
      await expect(page.getByTestId("update-details")).toContainText(labels.saveChanges);
      await expect(page.getByTestId("update-details")).toBeDisabled();

      await page.getByTestId("adapter-description").fill("Updated fixture description");
      await expect(page.getByTestId("update-details")).toBeEnabled();
      const settingsMetrics = await page.evaluate(() => {
        const drawer = document.querySelector<HTMLElement>(".adapter-settings-drawer");
        const wrapper = document.querySelector<HTMLElement>(".adapter-settings-drawer")?.parentElement;
        const body = document.querySelector<HTMLElement>(".adapter-settings-drawer .ant-drawer-body");
        return {
          settingsWidth: Math.round(wrapper?.getBoundingClientRect().width ?? 0),
          settingsBodyWidth: body?.scrollWidth ?? 0,
          settingsBodyClientWidth: body?.clientWidth ?? 0,
          drawerWidth: drawer?.getBoundingClientRect().width ?? 0,
        };
      });
      expect(settingsMetrics.settingsWidth).toBeGreaterThan(0);
      expect(settingsMetrics.settingsWidth).toBeLessThanOrEqual(520.5);
      expect(settingsMetrics.settingsBodyWidth).toBeLessThanOrEqual(settingsMetrics.settingsBodyClientWidth + 1);
      const settingsScreenshot = await captureScreenshot(page, `settings-${locale}-${width}`);
      const settingsBody = page.locator(".adapter-settings-drawer .ant-drawer-body");
      await settingsBody.evaluate((element) => {
        element.scrollTop = element.scrollHeight;
      });
      await page.getByTestId("adapter-danger-zone").scrollIntoViewIfNeeded();
      await expect(page.getByTestId("adapter-danger-zone")).toBeVisible();
      const settingsDangerScreenshot = await captureScreenshot(page, `settings-danger-${locale}-${width}`);

      expect(unknownRequests).toEqual([]);
      expect(consoleErrors).toEqual([]);
      expect(pageErrors).toEqual([]);
      expect(await page.locator("body").textContent()).not.toContain("FAKE_ADMIN_TOKEN");
      expect(await page.locator("html").getAttribute("lang")).toBe(locale);
      records.push({
        locale,
        width,
        screenshots: [catalogScreenshot, settingsScreenshot, settingsDangerScreenshot],
        overflow: {
          ...catalogMetrics,
          settingsWidth: settingsMetrics.settingsWidth,
          settingsBodyWidth: settingsMetrics.settingsBodyWidth,
        },
        consoleErrors,
        pageErrors,
        unknownRequests,
        language: locale,
      });
    });
  }
}

test.afterAll(() => {
  mkdirSync(evidenceRoot, { recursive: true });
  writeFileSync(
    resolve(evidenceRoot, "browser-report.json"),
    `${JSON.stringify({
      schema_version: 1,
      product: "DataLinkRuntime",
      wave: "M5.11 Wave V2 Adapter directory and settings",
      contract: "Issue #112 Scheme A",
      antd: "5.29.3",
      pro_components: "2.8.10",
      playwright: "1.62.1",
      browser: "chromium",
      viewport_widths: VIEWPORTS,
      locales: LOCALES,
      records,
    }, null, 2)}\n`,
    "utf8",
  );
});
