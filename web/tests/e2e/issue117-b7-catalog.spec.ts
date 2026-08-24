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
  process.env.DLR_B7_OUTPUT_DIR ?? "../../../docs/evidence/issue117-b7/auxiliary-matrix",
);
const screenshotDir = resolve(evidenceRoot, "browser");

const initialAdapters = [
  {
    id: 1,
    name: "manual-running",
    description: "searchable fixture",
    language: "python",
    adapter_type: "task",
    run_mode: "manual",
    timeout_seconds: 300,
    owner_user_id: 42,
    latest_version_id: null,
    runtime_worker_id: 1,
    runtime_locked: true,
    archived_at: null,
    running_execution_id: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    access_level: "admin",
  },
  {
    id: 2,
    name: "scheduled-stopped",
    description: "scheduled fixture",
    language: "python",
    adapter_type: "task",
    run_mode: "schedule",
    timeout_seconds: 300,
    owner_user_id: 42,
    latest_version_id: null,
    runtime_worker_id: 1,
    runtime_locked: false,
    archived_at: null,
    running_execution_id: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    access_level: "admin",
  },
  {
    id: 3,
    name: "webhook-stopped",
    description: "webhook fixture",
    language: "python",
    adapter_type: "webhook",
    run_mode: "manual",
    timeout_seconds: 300,
    owner_user_id: 42,
    latest_version_id: null,
    runtime_worker_id: 1,
    runtime_locked: false,
    archived_at: null,
    running_execution_id: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    access_level: "admin",
  },
  {
    id: 4,
    name: "manual-stopped",
    description: "manual fixture",
    language: "python",
    adapter_type: "task",
    run_mode: "manual",
    timeout_seconds: 300,
    owner_user_id: 42,
    latest_version_id: null,
    runtime_worker_id: 1,
    runtime_locked: false,
    archived_at: null,
    running_execution_id: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    access_level: "admin",
  },
];

interface CatalogRecord {
  locale: Locale;
  width: number;
  screenshot: string;
  overview_count: number;
  controls: {
    title: boolean;
    create: boolean;
    refresh: boolean;
    help: boolean;
    search: boolean;
    type_filter: boolean;
    status_filter: boolean;
  };
  states: {
    search_count: number;
    type_count: number;
    running_count: number;
    filtered_count: number;
    list_visible: boolean;
    select_dropdown_count: number;
    help_visible: boolean;
    help_closed: boolean;
    created: boolean;
  };
  geometry: {
    header_to_toolbar_gap: number;
    toolbar_to_list_gap: number;
    header_height: number;
    toolbar_height: number;
    list_top: number;
  };
  overflow: {
    inner_width: number;
    document_scroll_width: number;
    body_scroll_width: number;
  };
  requests: {
    adapter_list_count: number;
    create_count: number;
    non_get_paths: string[];
    unknown_paths: string[];
  };
  console_errors: string[];
  page_errors: string[];
}

const records: CatalogRecord[] = [];
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

function labels(locale: Locale) {
  return locale === "zh-CN"
    ? {
        catalog: "适配器",
        toolbar: "适配器工具栏",
        search: "搜索适配器",
        type: "适配器类型筛选",
        status: "适配器状态筛选",
        taskManual: "任务型（手动）",
        filterTypeAll: "全部类型",
        filterStatusAll: "全部状态",
        running: "运行中",
        refresh: "刷新适配器列表",
        help: "适配器目录帮助",
        helpText: "搜索名称或描述；类型和状态筛选可以叠加使用。",
        create: "创建",
      }
    : {
        catalog: "Adapters",
        toolbar: "Adapter toolbar",
        search: "Search Adapters",
        type: "Adapter type filter",
        status: "Adapter status filter",
        taskManual: "Task (manual)",
        filterTypeAll: "All types",
        filterStatusAll: "All statuses",
        running: "Running",
        refresh: "Refresh Adapter list",
        help: "Adapter catalog help",
        helpText: "Search by name or description; type and status filters can be combined.",
        create: "Create",
      };
}

async function installFixture(page: Page, locale: Locale): Promise<{
  adapterListCalls: { value: number };
  createBodies: string[];
  nonGetPaths: string[];
  unknownPaths: string[];
  adapters: typeof initialAdapters;
}> {
  const adapterListCalls = { value: 0 };
  const createBodies: string[] = [];
  const nonGetPaths: string[] = [];
  const unknownPaths: string[] = [];
  const adapters = structuredClone(initialAdapters);

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
    if (method !== "GET") {
      nonGetPaths.push(`${method} ${path}`);
    }

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
      await fulfillJson(route, [{
        id: 1,
        name: "batch-7-fixture-worker",
        status: "online",
        last_heartbeat: "2026-01-01T00:00:00Z",
        capabilities: ["python"],
      }]);
      return;
    }
    if (path === "/api/adapters" && method === "GET") {
      adapterListCalls.value += 1;
      await fulfillJson(route, adapters);
      return;
    }
    if (path === "/api/adapters" && method === "POST") {
      const body = request.postData() ?? "";
      createBodies.push(body);
      const payload = JSON.parse(body) as {
        name: string;
        description: string;
        language: string;
        adapter_type: string;
      };
      const created = {
        ...adapters[0],
        id: 5,
        name: payload.name,
        description: payload.description,
        language: payload.language,
        adapter_type: payload.adapter_type,
        runtime_locked: false,
      };
      adapters.push(created);
      await fulfillJson(route, created, 201);
      return;
    }
    if (/^\/api\/adapters\/\d+\/versions$/.test(path) && method === "GET") {
      await fulfillJson(route, []);
      return;
    }

    const requestKey = `${method} ${path}${url.search}`;
    unknownPaths.push(requestKey);
    await fulfillJson(route, { detail: { code: "issue117_b7_unhandled_request", message: requestKey } }, 404);
  });

  return { adapterListCalls, createBodies, nonGetPaths, unknownPaths, adapters };
}

async function login(page: Page, locale: Locale): Promise<void> {
  await expect(page.getByRole("heading", {
    name: locale === "zh-CN" ? "欢迎登录 DLR 控制台" : "Welcome to the DLR Console",
  })).toBeVisible();
  await page.getByTestId("admin-token-input").fill("FAKE_ADMIN_TOKEN");
  await page.getByTestId("admin-token-submit").click();
  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
}

async function selectOption(page: Page, testId: string, option: string): Promise<void> {
  await page.getByTestId(testId).locator(".ant-select-selector").click();
  const input = page.getByTestId(testId).locator('input[role="combobox"]');
  await expect(input).toBeFocused();
  const optionLocator = page.getByRole("option", { name: option, exact: true });
  const optionId = await optionLocator.getAttribute("id");
  expect(optionId).toBeTruthy();
  await expect(input).toHaveAttribute("aria-expanded", "true");
  for (let attempt = 0; attempt < 4; attempt += 1) {
    if ((await input.getAttribute("aria-activedescendant")) === optionId) {
      break;
    }
    await input.press("ArrowDown");
    await expect.poll(() => input.getAttribute("aria-activedescendant")).not.toBeNull();
  }
  await expect(input).toHaveAttribute("aria-activedescendant", optionId!);
  await input.press("Enter");
  await expect(page.getByTestId(testId)).toContainText(option);
}

async function closeSelectDropdowns(page: Page) {
  const openDropdowns = page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)");
  await page.keyboard.press("Escape");
  await page.getByTestId("adapter-catalog-header").locator(".catalog-title").click();
  await expect(openDropdowns).toHaveCount(0);
  return openDropdowns;
}

async function runCase(page: Page, locale: Locale, width: number): Promise<void> {
  const text = labels(locale);
  const { adapterListCalls, createBodies, nonGetPaths, unknownPaths, adapters } =
    await installFixture(page, locale);
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await page.goto("/");
  await login(page, locale);

  const header = page.getByTestId("adapter-catalog-header");
  const toolbar = page.getByRole("toolbar", { name: text.toolbar });
  const search = toolbar.getByRole("textbox", { name: text.search });
  const typeFilter = toolbar.getByRole("combobox", { name: text.type });
  const statusFilter = toolbar.getByRole("combobox", { name: text.status });
  await expect(header.getByRole("heading", { name: text.catalog })).toBeVisible();
  await expect(page.getByTestId("adapter-catalog-description")).toHaveCount(0);
  await expect(search).toBeVisible();
  await expect(typeFilter).toBeVisible();
  await expect(statusFilter).toBeVisible();
  await expect(page.getByTestId("adapter-type-filter")).toContainText(text.filterTypeAll);
  await expect(page.getByTestId("adapter-status-filter")).toContainText(text.filterStatusAll);
  const createEntry = page.getByTestId("show-create-form");
  await expect(createEntry).toBeVisible();
  await expect(page.getByTestId("refresh-adapters")).toHaveAttribute("aria-label", text.refresh);
  await expect(page.getByTestId("adapter-catalog-help")).toHaveAttribute("aria-label", text.help);

  await page.getByTestId("adapter-catalog-help").click();
  await expect(page.getByText(text.helpText)).toBeVisible();
  await page.getByTestId("app-header").click();
  await expect(page.getByText(text.helpText)).toBeHidden();

  await search.fill("searchable");
  await expect(page.getByTestId("adapter-item")).toHaveCount(1);
  const searchCount = await page.getByTestId("adapter-item").count();
  await search.fill("");
  await selectOption(page, "adapter-type-filter", text.taskManual);
  await expect(page.getByTestId("adapter-item")).toHaveCount(2);
  const typeCount = await page.getByTestId("adapter-item").count();
  await selectOption(page, "adapter-status-filter", text.running);
  await expect(page.getByTestId("adapter-item")).toHaveCount(1);
  const runningCount = await page.getByTestId("adapter-item").count();
  const filteredCount = await page.getByTestId("adapter-item").count();

  const refresh = page.getByTestId("refresh-adapters");
  await refresh.focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => adapterListCalls.value).toBeGreaterThan(1);

  const openDropdowns = await closeSelectDropdowns(page);
  const visibleItems = page.getByTestId("adapter-item");
  await expect(visibleItems).toHaveCount(1);
  const listVisible = await visibleItems.first().isVisible();
  const selectDropdownCount = await openDropdowns.count();
  expect(listVisible).toBe(true);
  expect(selectDropdownCount).toBe(0);

  const geometry = await page.evaluate(() => {
    const header = document.querySelector<HTMLElement>("[data-testid=adapter-catalog-header]");
    const toolbar = document.querySelector<HTMLElement>("[data-testid=adapter-catalog-toolbar]");
    const list = document.querySelector<HTMLElement>("[data-testid=adapter-catalog-list]");
    const headerRect = header?.getBoundingClientRect();
    const toolbarRect = toolbar?.getBoundingClientRect();
    const listRect = list?.getBoundingClientRect();
    return {
      header_to_toolbar_gap: Math.round((toolbarRect?.top ?? 0) - (headerRect?.bottom ?? 0)),
      toolbar_to_list_gap: Math.round((listRect?.top ?? 0) - (toolbarRect?.bottom ?? 0)),
      header_height: Math.round(headerRect?.height ?? 0),
      toolbar_height: Math.round(toolbarRect?.height ?? 0),
      list_top: Math.round(listRect?.top ?? 0),
      overview_count: document.querySelectorAll("[data-testid=adapter-catalog-description]").length,
      inner_width: window.innerWidth,
      document_scroll_width: document.documentElement.scrollWidth,
      body_scroll_width: document.body.scrollWidth,
    };
  });
  expect(geometry.header_to_toolbar_gap).toBeLessThanOrEqual(1);
  expect(geometry.header_to_toolbar_gap).toBeGreaterThanOrEqual(-1);
  expect(geometry.toolbar_to_list_gap).toBeLessThanOrEqual(1);
  expect(geometry.toolbar_to_list_gap).toBeGreaterThanOrEqual(-1);
  expect(geometry.overview_count).toBe(0);
  expect(geometry.document_scroll_width).toBeLessThanOrEqual(geometry.inner_width);
  expect(geometry.body_scroll_width).toBeLessThanOrEqual(geometry.inner_width);

  const screenshotName = `catalog-${locale}-${width}.png`;
  mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({ path: resolve(screenshotDir, screenshotName), fullPage: true });

  await createEntry.click();
  await expect(page.getByTestId("new-adapter-name")).toBeVisible();
  await expect
    .poll(async () => (await page.getByTestId("create-adapter").innerText()).replace(/\s+/g, ""))
    .toBe(text.create);
  await page.getByTestId("new-adapter-name").fill("created-in-batch-7");
  await page.getByTestId("new-adapter-description").fill("created fixture");
  await page.getByTestId("create-adapter").click();
  await expect(page.getByTestId("workbench-header")).toBeVisible();
  expect(createBodies).toHaveLength(1);
  expect(JSON.parse(createBodies[0])).toEqual({
    name: "created-in-batch-7",
    description: "created fixture",
    language: "python",
    adapter_type: "task",
  });
  expect(adapters.some((adapter) => adapter.name === "created-in-batch-7")).toBe(true);
  expect(nonGetPaths).toEqual(["POST /api/adapters"]);
  expect(unknownPaths).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
  expect(await page.locator("body").textContent()).not.toContain("FAKE_ADMIN_TOKEN");
  expect(await page.locator("html").getAttribute("lang")).toBe(locale);

  records.push({
    locale,
    width,
    screenshot: `docs/evidence/issue117-b7/auxiliary-matrix/browser/${screenshotName}`,
    overview_count: geometry.overview_count,
    controls: {
      title: true,
      create: true,
      refresh: true,
      help: true,
      search: true,
      type_filter: true,
      status_filter: true,
    },
    states: {
      search_count: searchCount,
      type_count: typeCount,
      running_count: runningCount,
      filtered_count: filteredCount,
      list_visible: listVisible,
      select_dropdown_count: selectDropdownCount,
      help_visible: true,
      help_closed: true,
      created: true,
    },
    geometry: {
      header_to_toolbar_gap: geometry.header_to_toolbar_gap,
      toolbar_to_list_gap: geometry.toolbar_to_list_gap,
      header_height: geometry.header_height,
      toolbar_height: geometry.toolbar_height,
      list_top: geometry.list_top,
    },
    overflow: {
      inner_width: geometry.inner_width,
      document_scroll_width: geometry.document_scroll_width,
      body_scroll_width: geometry.body_scroll_width,
    },
    requests: {
      adapter_list_count: adapterListCalls.value,
      create_count: createBodies.length,
      non_get_paths: nonGetPaths,
      unknown_paths: unknownPaths,
    },
    console_errors: consoleErrors,
    page_errors: pageErrors,
  });
}

test.afterAll(() => {
  mkdirSync(evidenceRoot, { recursive: true });
  records.sort((left, right) => left.locale.localeCompare(right.locale) || left.width - right.width);
  writeFileSync(
    resolve(evidenceRoot, "browser-report.json"),
    `${JSON.stringify({
      schema_version: 1,
      product: "DataLinkRuntime",
      dispatch_id: "issue117-b7-catalog-20260825-r1",
      batch: "7",
      scope: "Adapter Catalog overview removal and compact layout",
      antd: "5.29.3",
      playwright: "1.62.1",
      browser: "chromium",
      browser_version: browserVersion,
      viewport_widths: VIEWPORTS,
      locales: LOCALES,
      fixture_provider: "scoped Playwright route fixture",
      real_provider_credentials: false,
      raw_provider_response_archived: false,
      screenshot_capture: {
        dropdowns_closed_before_capture: true,
        list_visibility_sampled_before_capture: true,
        record_reuses_pre_screenshot_samples: true,
      },
      records,
    }, null, 2)}\n`,
    "utf8",
  );
});

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`${locale} ${width}px Adapter Catalog Batch 7 contract`, async ({ browser }) => {
      browserVersion = browser.version();
      const context = await browser.newContext({ viewport: { width, height: 800 } });
      const page = await context.newPage();
      await runCase(page, locale, width);
      await page.close();
      await context.close();
    });
  }
}
