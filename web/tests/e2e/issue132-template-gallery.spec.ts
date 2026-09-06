import { expect, test, type Page, type Route } from "@playwright/test";

type Language = "python" | "javascript" | "java";

const themes = [
  ["cloud-cmdb", "云与 CMDB", 7],
  ["api-events", "API 与事件", 3],
  ["file-data", "文件与数据", 3],
  ["databases", "数据库", 2],
  ["storage-transfer", "存储与传输", 2],
].map(([slug, name, count], index) => ({
  slug,
  name: { "zh-CN": name, en: String(name) },
  description: { "zh-CN": `${name}模板`, en: `${name} templates` },
  sort_order: (index + 1) * 10,
  scenario_count: count,
}));

const logoKeys = [
  "alicloud-compute",
  "alicloud-network",
  "alicloud-data",
  "tencentcloud-compute",
  "tencentcloud-network",
  "tencentcloud-data",
  "servicenow-cmdb",
  "rest-request",
  "rest-pagination",
  "webhook-normalize",
  "file-csv",
  "file-excel",
  "data-json",
  "database-postgresql",
  "database-mysql",
  "storage-s3",
  "transfer-sftp",
];

function scenario(index: number) {
  const slug = index === 0 ? "rest-single-request" : `fixture-scenario-${index + 1}`;
  return {
    slug,
    theme_slug: "cloud-cmdb",
    title: { "zh-CN": index === 0 ? "REST 单次请求" : `云模板 ${String(index + 1).padStart(2, "0")}`, en: `Cloud template ${index + 1}` },
    summary: { "zh-CN": "读取接口数据并返回 JSON。", en: "Read API data and return JSON." },
    vendor: index % 2 === 0 ? "DLR" : "Alibaba Cloud",
    adapter_type: "task" as const,
    protocols: index % 2 === 0 ? ["HTTP", "JSON"] : ["OpenAPI", "JSON"],
    tags: ["fixture", `tag-${index + 1}`],
    logo_key: logoKeys[index % logoKeys.length],
    template_version: "1.0.0",
    updated_at: "2026-09-05",
    variants: (["python", "javascript", "java"] as const).map((language) => ({
      language,
      available: true,
    })),
  };
}

const scenarios = Array.from({ length: 13 }, (_, index) => scenario(index));

function detail(slug = "rest-single-request") {
  const base = slug === "csv-to-json"
    ? { ...scenario(10), slug, theme_slug: "file-data", title: { "zh-CN": "CSV 转 JSON", en: "CSV to JSON" }, logo_key: "file-csv" }
    : scenarios[0];
  return {
    ...base,
    details: { "zh-CN": "修改代码开头的请求地址，保存后运行。", en: "Set the URL at the start of the code, save, and run." },
  };
}

function variant(slug: string, language: Language) {
  return {
    scenario_slug: slug,
    theme_slug: slug === "csv-to-json" ? "file-data" : "cloud-cmdb",
    title: detail(slug).title,
    language,
    adapter_type: "task",
    template_version: "1.0.0",
    code: `${language} template code for ${slug}\n`,
    requirements: language === "python" ? "httpx==0.28.1" : "",
    input_skeleton: slug === "csv-to-json" ? { file: "example.csv" } : {},
    output_example: { status: 200, data: [{ id: "example" }] },
    runtime_config: {},
  };
}

const existingAdapter = {
  id: 1,
  name: "现有适配器",
  description: "dirty draft fixture",
  language: "python",
  adapter_type: "task",
  run_mode: "manual",
  timeout_seconds: 300,
  owner_user_id: null,
  owner_username: null,
  latest_version_id: 10,
  runtime_worker_id: null,
  template_scenario_slug: null,
  template_version: null,
  runtime_locked: false,
  archived_at: null,
  running_execution_id: null,
  created_at: "2026-09-05T00:00:00Z",
  updated_at: "2026-09-05T00:00:00Z",
  access_level: "admin",
};

const createdAdapter = {
  ...existingAdapter,
  id: 42,
  name: "生产 REST 同步",
  description: "copied fixture",
  latest_version_id: null,
  template_scenario_slug: null,
  template_version: null,
};

const version = {
  id: 10,
  adapter_id: 1,
  seq: 1,
  code: "def handle(context, input):\n    return input\n",
  requirements: "",
  runtime_config: {},
  created_at: "2026-09-05T00:00:00Z",
};

interface FixtureState {
  created: boolean;
  createdName: string | null;
  createdWorkerId: number | null;
  savedPayload: { code: string; requirements: string; runtime_config: Record<string, unknown> } | null;
  posts: string[];
  unknown: string[];
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function installFixture(page: Page): Promise<FixtureState> {
  const state: FixtureState = { created: false, createdName: null, createdWorkerId: null, savedPayload: null, posts: [], unknown: [] };
  const currentCreatedAdapter = () => ({
    ...createdAdapter,
    name: state.createdName ?? createdAdapter.name,
    runtime_worker_id: state.createdWorkerId,
    latest_version_id: state.savedPayload ? 77 : null,
  });
  await page.addInitScript(() => {
    window.sessionStorage.setItem("dlr-admin-token", "ISSUE132_BROWSER_TOKEN");
    window.localStorage.setItem("dlr-system-locale", "zh-CN");
  });
  await page.route("**/entry-mode.js", (route) => route.fulfill({
    contentType: "application/javascript",
    body: 'window.__DLR_ENTRY_MODE__ = "token";',
  }));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;

    if (method === "POST") state.posts.push(path);
    if (method === "GET" && path === "/api/locale") return json(route, { locale: "zh-CN" });
    if (method === "GET" && path === "/api/health") return json(route, { status: "ok", database: true });
    if (method === "GET" && path === "/api/workers") {
      return json(route, [{
        id: 1,
        name: "fixture-worker",
        status: "online",
        last_heartbeat: "2099-01-01T00:00:00Z",
        capabilities: ["python", "javascript", "java"],
        protocol_version: 3,
        isolation_preflight_status: "passed",
        rabbitmq_execution_v3: true,
        isolation_capabilities: {
          cgroup_v2: true,
          cgroup_namespace_private: true,
          mount_namespace: true,
          pid_namespace: true,
          memory_hard_limit: true,
          pids_hard_limit: true,
          tmpfs_hard_limit: true,
          bounded_output: true,
          preflight_passed: true,
          resource_envelope_verified: true,
          cpu_hard_limit: true,
          swap_hard_limit: true,
          nofile_hard_limit: true,
          no_new_privileges: true,
          cgroup_kill: true,
          adapter_control_plane_hidden: true,
          adapter_mount_blocked: true,
          sandbox_cleanup: true,
        },
      }]);
    }
    if (method === "GET" && path === "/api/system/managed-input-capability") {
      return json(route, {
        managed_files_enabled: false,
        ready: false,
        default_retention_seconds: 86_400,
        max_custom_retention_seconds: 2_592_000,
        allow_manual_delete: true,
        allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"],
      });
    }
    if (method === "GET" && path === "/api/adapters") {
      return json(route, state.created
        ? [currentCreatedAdapter(), existingAdapter]
        : [existingAdapter]);
    }
    if (method === "GET" && path === "/api/templates/themes") return json(route, themes);
    if (method === "GET" && path === "/api/templates/scenarios") {
      const theme = url.searchParams.get("theme");
      let selected = theme === "file-data" ? [detail("csv-to-json")] : [...scenarios];
      const query = url.searchParams.get("q")?.toLocaleLowerCase("zh-CN") ?? "";
      const vendor = url.searchParams.get("vendor");
      const protocol = url.searchParams.get("protocol");
      if (query) selected = selected.filter((item) => JSON.stringify(item).toLocaleLowerCase("zh-CN").includes(query));
      if (vendor) selected = selected.filter((item) => item.vendor === vendor);
      if (protocol) selected = selected.filter((item) => item.protocols.includes(protocol));
      const pageNumber = Number(url.searchParams.get("page") ?? "1");
      const pageSize = Number(url.searchParams.get("page_size") ?? "12");
      return json(route, {
        items: selected.slice((pageNumber - 1) * pageSize, pageNumber * pageSize),
        page: pageNumber,
        page_size: pageSize,
        total: selected.length,
      });
    }
    const detailMatch = /^\/api\/templates\/scenarios\/([^/]+)$/.exec(path);
    if (method === "GET" && detailMatch) return json(route, detail(detailMatch[1]));
    const variantMatch = /^\/api\/templates\/scenarios\/([^/]+)\/variants\/(python|javascript|java)$/.exec(path);
    if (method === "GET" && variantMatch) return json(route, variant(variantMatch[1], variantMatch[2] as Language));
    const instantiateMatch = /^\/api\/templates\/scenarios\/([^/]+)\/variants\/(python|javascript|java)\/instantiate$/.exec(path);
    if (method === "POST" && instantiateMatch) {
      const payload = request.postDataJSON() as { name: string };
      if (payload.name === "冲突名称") {
        return json(route, { detail: { code: "adapter_name_conflict", message: "conflict" } }, 409);
      }
      state.created = true;
      state.createdName = payload.name;
      return json(route, { ...createdAdapter, name: payload.name }, 201);
    }
    if (method === "GET" && path === "/api/adapters/1/versions") {
      return json(route, [{ id: 10, adapter_id: 1, seq: 1, created_at: version.created_at }]);
    }
    if (method === "GET" && path === "/api/adapters/1/versions/10") return json(route, version);
    if (method === "PATCH" && path === "/api/adapters/42") {
      state.createdWorkerId = (request.postDataJSON() as { runtime_worker_id: number }).runtime_worker_id;
      return json(route, currentCreatedAdapter());
    }
    if (method === "POST" && path === "/api/adapters/42/versions") {
      state.savedPayload = request.postDataJSON();
      return json(route, { ...version, ...state.savedPayload, id: 77, adapter_id: 42 }, 201);
    }
    if (method === "GET" && path === "/api/adapters/42") return json(route, currentCreatedAdapter());
    if (method === "GET" && path === "/api/adapters/42/versions") {
      return json(route, state.savedPayload ? [{ id: 77, adapter_id: 42, seq: 1, created_at: version.created_at }] : []);
    }
    if (method === "GET" && /^\/api\/adapters\/\d+\/input-config$/.test(path)) {
      return json(route, {
        adapter_id: Number(path.split("/")[3]),
        revision: 1,
        source_type: "none",
        json_value: null,
        retention: { mode: "system_default", seconds: null },
        artifacts: [],
        valid_for_run: true,
        invalid_reason: null,
      });
    }
    if (method === "GET" && /^\/api\/adapters\/\d+\/schedule$/.test(path)) {
      return json(route, { detail: { code: "schedule_not_found", message: "not found" } }, 404);
    }
    if (method === "GET" && /^\/api\/adapters\/\d+\/executions/.test(path)) {
      return json(route, { items: [], next_before_id: null });
    }
    if (method === "GET" && /^\/api\/adapters\/\d+\/credential-bindings$/.test(path)) return json(route, []);
    if (method === "GET" && path === "/api/credentials") return json(route, []);

    const key = `${method} ${path}${url.search}`;
    state.unknown.push(key);
    return json(route, { detail: { code: "unhandled_browser_fixture", message: key } }, 404);
  });
  return state;
}

function monitorErrors(page: Page): { consoleErrors: string[]; pageErrors: string[] } {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  return { consoleErrors, pageErrors };
}

test("Gallery search, filters, pagination and responsive layouts remain bounded", async ({ page }, testInfo) => {
  const state = await installFixture(page);
  const errors = monitorErrors(page);
  await page.setViewportSize({ width: 1680, height: 900 });
  await page.goto("/templates");
  await expect(page.getByRole("heading", { name: "模板广场" })).toBeVisible();
  await expect(page.locator(".template-card")).toHaveCount(12);
  await expect(page.getByRole("tab", { name: /全部/ })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("combobox", { name: "成熟度" })).toHaveCount(0);
  await page.getByTitle("2").click();
  await expect(page.locator(".template-card")).toHaveCount(1);
  await page.getByRole("tab", { name: /API 与事件/ }).click();
  await expect(page.locator(".template-card")).toHaveCount(12);
  await page.getByRole("tab", { name: /全部/ }).click();
  await expect(page.locator(".template-card")).toHaveCount(1);
  await page.getByRole("textbox", { name: "搜索模板" }).fill("REST 单次请求");
  await expect(page.getByRole("heading", { name: "REST 单次请求" })).toBeVisible();

  await page.getByRole("textbox", { name: "搜索模板" }).fill("");
  await page.getByRole("combobox", { name: "厂商" }).click();
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter({ hasText: "Alibaba Cloud" }).click();
  await expect(page.locator(".template-card")).toHaveCount(6);
  await page.getByRole("heading", { name: "模板广场" }).click();
  await expect(page.locator(".ant-select-dropdown:visible")).toHaveCount(0);

  for (const [width, expectedColumns] of [[1680, 3], [1280, 3], [900, 2], [560, 1]] as const) {
    await page.setViewportSize({ width, height: width === 560 ? 820 : 900 });
    await expect.poll(() => page.locator(".template-gallery").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    await expect.poll(() => page.locator(".template-card").evaluateAll((cards) => {
      if (cards.length === 0) return 0;
      const top = cards[0].getBoundingClientRect().top;
      return cards.filter((card) => Math.abs(card.getBoundingClientRect().top - top) < 2).length;
    })).toBe(expectedColumns);
    await page.screenshot({ path: testInfo.outputPath(`gallery-${width}.png`), fullPage: true });
  }

  await page.setViewportSize({ width: 560, height: 820 });
  await page.goto("/templates/rest-single-request");
  await expect(page.getByText("python template code for rest-single-request")).toBeVisible();
  await expect.poll(() => page.locator(".template-detail").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  await expect.poll(() => page.locator(".template-detail-layout").evaluate((element) => (
    getComputedStyle(element).gridTemplateColumns.trim().split(/\s+/).filter(Boolean).length
  ))).toBe(1);
  await page.screenshot({ path: testInfo.outputPath("template-detail-560.png"), fullPage: true });
  expect(state.unknown).toEqual([]);
  expect(errors.consoleErrors).toEqual([]);
  expect(errors.pageErrors).toEqual([]);
});

test("a conflict keeps the name and a successful copy opens the new Adapter editor", async ({ page }, testInfo) => {
  const state = await installFixture(page);
  const errors = monitorErrors(page);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/templates/rest-single-request");
  await expect(page.getByText("python template code for rest-single-request")).toBeVisible();
  const copyButton = page.getByRole("button", { name: "复制为适配器" });
  await expect(copyButton).toBeEnabled();
  await expect.poll(() => copyButton.evaluate((element) => getComputedStyle(element).backgroundColor))
    .toBe("rgb(9, 88, 217)");
  await expect(page.getByRole("heading", { name: "输入示例" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "返回结果示例" })).toBeVisible();
  for (const name of ["运行模式", "安全边界", "来源与许可证", "Runtime 建议配置", "各语言成熟度"]) {
    await expect(page.getByRole("heading", { name })).toHaveCount(0);
  }
  await page.screenshot({ path: testInfo.outputPath("template-detail-1280.png"), fullPage: true });
  await page.getByRole("tab", { name: "JavaScript" }).click();
  await expect(page.getByText("javascript template code for rest-single-request")).toBeVisible();
  await page.getByRole("tab", { name: "Python" }).click();
  await copyButton.click();
  const name = page.getByRole("textbox", { name: "适配器名称" });
  await expect(name).toBeFocused();
  await name.fill("冲突名称");
  await page.getByRole("button", { name: "复制并编辑" }).click();
  await expect(page.getByText("已有同名适配器，请换一个名称。")).toBeVisible();
  await expect(name).toHaveValue("冲突名称");
  await name.fill("生产 REST 同步");
  await page.getByRole("button", { name: "复制并编辑" }).click();

  await expect(page).toHaveURL(/\/adapters$/);
  await expect(page.getByRole("heading", { name: "生产 REST 同步" })).toBeVisible();
  await expect(page.locator(".monaco-editor")).toContainText("python template code for rest-single-request");
  await expect(page.getByRole("button", { name: "保存", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "运行一次", exact: true })).toBeDisabled();
  expect(state.posts.some((path) => path.endsWith("/versions"))).toBe(false);
  await page.screenshot({ path: testInfo.outputPath("copied-adapter-edit.png"), fullPage: true });
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => state.savedPayload).toEqual({
    code: variant("rest-single-request", "python").code,
    requirements: "httpx==0.28.1",
    runtime_config: {},
  });
  await expect(page.getByRole("button", { name: "运行一次", exact: true })).toBeEnabled();
  expect(state.posts.filter((path) => path.endsWith("/instantiate"))).toHaveLength(2);
  expect(state.posts.some((path) => path.includes("clone"))).toBe(false);
  expect(state.unknown).toEqual([]);
  expect(errors.consoleErrors.filter((message) => !message.includes("409 (Conflict)"))).toEqual([]);
  expect(errors.consoleErrors.filter((message) => message.includes("409 (Conflict)"))).toHaveLength(1);
  expect(errors.pageErrors).toEqual([]);
});

test("Task input draft cancellation performs no POST and file templates stay copyable", async ({ page }) => {
  const state = await installFixture(page);
  const errors = monitorErrors(page);
  await page.goto("/adapters");
  await page.getByTestId("adapter-item").first().click();
  await page.getByRole("tab", { name: "运行设置" }).click();
  await page.getByTestId("task-input-source-json").click();
  await page.getByTestId("task-input-json").fill('{"dirty":"runtime-input"}');
  await page.getByRole("link", { name: "模板广场" }).click();
  await page.getByRole("tab", { name: /文件与数据/ }).click();
  await page.getByRole("link", { name: "查看详情" }).click();
  await expect(page.getByRole("heading", { name: "输入示例" })).toBeVisible();
  await page.getByRole("button", { name: "复制为适配器" }).click();
  await page.getByRole("textbox", { name: "适配器名称" }).fill("不会创建");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "复制并编辑" }).click();
  await expect(page.getByRole("textbox", { name: "适配器名称" })).toHaveValue("不会创建");
  expect(state.posts.filter((path) => path.endsWith("/instantiate"))).toEqual([]);
  await page.getByRole("button", { name: /取\s*消/ }).click();
  await page.getByRole("link", { name: "适配器" }).click();
  await expect(page.getByTestId("task-input-json")).toHaveValue('{"dirty":"runtime-input"}');
  expect(state.unknown).toEqual([]);
  expect(errors.consoleErrors).toEqual([]);
  expect(errors.pageErrors).toEqual([]);
});

test("keyboard navigation opens copy, traps focus and restores the trigger", async ({ page }) => {
  const state = await installFixture(page);
  const errors = monitorErrors(page);
  await page.goto("/templates");

  const cloudTab = page.getByRole("tab", { name: /云与 CMDB/ });
  await cloudTab.focus();
  await cloudTab.press("ArrowRight");
  const apiTab = page.getByRole("tab", { name: /API 与事件/ });
  await expect(apiTab).toBeFocused();
  await apiTab.press("Enter");
  await expect(apiTab).toHaveAttribute("aria-selected", "true");

  const detailLink = page.getByRole("link", { name: "查看详情" }).first();
  await detailLink.focus();
  await detailLink.press("Enter");
  await expect(page).toHaveURL(/\/templates\/rest-single-request$/);
  await expect(page.locator(".template-detail")).toBeFocused();

  const copyButton = page.getByRole("button", { name: "复制为适配器" });
  await expect(copyButton).toBeEnabled();
  await copyButton.focus();
  await copyButton.press("Space");
  const name = page.getByRole("textbox", { name: "适配器名称" });
  await expect(name).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(copyButton).toBeFocused();

  await copyButton.press("Enter");
  await expect(name).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("textbox", { name: "描述（可选）" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(name).toBeFocused();
  await name.fill("键盘创建副本");
  await name.press("Enter");
  await expect(page).toHaveURL(/\/adapters$/);
  await expect(page.getByRole("heading", { name: "键盘创建副本" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.activeElement?.closest(".monaco-editor") !== null)).toBe(true);

  expect(state.posts.filter((path) => path.endsWith("/instantiate"))).toHaveLength(1);
  expect(state.unknown).toEqual([]);
  expect(errors.consoleErrors).toEqual([]);
  expect(errors.pageErrors).toEqual([]);
});
