import { expect, test, type Page, type Route } from "@playwright/test";

type Locale = "zh-CN" | "en";
type RuntimeMode = "normal" | "warning" | "error";

const LOCALES: readonly Locale[] = ["zh-CN", "en"];
const VIEWPORTS = [1280, 1440, 1680, 1920] as const;

const readyWorker = {
  id: 1,
  name: "runtime-worker-a",
  status: "online",
  last_heartbeat: "2026-09-04T00:00:00Z",
  capabilities: ["python", "javascript", "java"],
  protocol_version: 3,
  isolation_preflight_status: "passed",
  isolation_preflight_at: "2026-09-04T00:00:00Z",
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

const healthyControl = {
  service: "dlr-control",
  status: "ok",
  database: true,
  rabbitmq: {
    enabled: true,
    status: "ready",
    ready: true,
    ingress: { enabled: true, status: "ready", ready: true },
    repair: { configured: true, status: "ready", ready: true, worker_count: 1 },
    broker: {
      queue_max_length: 2_000,
      queue_max_bytes: 67_108_864,
      headroom_messages: 1_980,
      headroom_bytes: 66_781_184,
      alerts: [],
    },
  },
  outbox: {
    status: "ok",
    pending_count: 0,
    pending_bytes: 0,
    oldest_age_seconds: 0,
  },
};

function jsonBody(body: unknown): string {
  return JSON.stringify(body);
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: jsonBody(body) });
}

async function installRoutes(
  page: Page,
  locale: Locale,
  runtime: { mode: RuntimeMode; healthCalls: number; workerCalls: number },
): Promise<string[]> {
  const unknownRequests: string[] = [];
  await page.route("**/entry-mode.js", async (route) => {
    await route.fulfill({
      contentType: "application/javascript",
      body: "window.__DLR_ENTRY_MODE__ = 'token';",
    });
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/locale" && method === "GET") {
      await fulfillJson(route, { locale });
      return;
    }
    if (path === "/api/auth/admin/verify" && method === "GET") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/health" && method === "GET") {
      runtime.healthCalls += 1;
      await fulfillJson(
        route,
        runtime.mode === "error"
          ? { ...healthyControl, status: "degraded", database: false }
          : healthyControl,
      );
      return;
    }
    if (path === "/api/workers" && method === "GET") {
      runtime.workerCalls += 1;
      await fulfillJson(
        route,
        runtime.mode === "warning"
          ? [readyWorker, { ...readyWorker, id: 2, name: "runtime-worker-b", status: "offline" }]
          : [readyWorker],
      );
      return;
    }
    if (path === "/api/adapters" && method === "GET") {
      await fulfillJson(route, []);
      return;
    }

    const requestKey = `${method} ${path}${url.search}`;
    unknownRequests.push(requestKey);
    await fulfillJson(route, { detail: { code: "unhandled_test_request", message: requestKey } }, 404);
  });
  return unknownRequests;
}

async function seedAuthenticatedLocale(page: Page, locale: Locale) {
  await page.addInitScript((selectedLocale) => {
    window.sessionStorage.setItem("dlr-admin-token", "SYSTEM_STATUS_TEST_TOKEN");
    window.localStorage.setItem("dlr-system-locale", selectedLocale);
  }, locale);
}

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`Issue #130 System Status ${locale} ${width}px`, async ({ page }, testInfo) => {
      const height = width === 1280 ? 720 : width === 1440 ? 800 : width === 1680 ? 900 : 1080;
      await page.setViewportSize({ width, height });
      const consoleErrors: string[] = [];
      const pageErrors: string[] = [];
      page.on("console", (message) => {
        if (message.type() === "error") consoleErrors.push(message.text());
      });
      page.on("pageerror", (error) => pageErrors.push(error.message));
      const runtime = { mode: "normal" as RuntimeMode, healthCalls: 0, workerCalls: 0 };
      const unknownRequests = await installRoutes(page, locale, runtime);
      await seedAuthenticatedLocale(page, locale);

      await page.goto("/");
      const summary = page.getByTestId("system-status-summary");
      await expect(summary).toHaveText(locale === "zh-CN" ? "系统正常" : "System normal");
      await expect(summary.locator(".ant-badge-status-success")).toBeVisible();
      await expect(page.getByTestId("worker-status-details")).toHaveCount(0);
      await expect(page.getByTestId("worker-status")).toHaveCount(0);
      await expect(page.getByTestId("control-status")).toHaveCount(0);

      await summary.click();
      await expect(page).toHaveURL(/\/settings\/system-status$/);
      await expect(page.getByRole("heading", {
        level: 3,
        name: locale === "zh-CN" ? "系统状态" : "System Status",
      })).toBeVisible();
      await expect(page.getByText(locale === "zh-CN" ? "控制节点" : "Control node", { exact: true })).toBeVisible();
      await expect(page.getByText(locale === "zh-CN" ? "可靠运行时" : "Reliable runtime", { exact: true })).toBeVisible();
      await expect(page.getByText("runtime-worker-a")).toBeVisible();
      await expect(page.getByText(locale === "zh-CN" ? "隔离预检通过" : "Isolation preflight passed")).toBeVisible();

      const overflow = await page.evaluate(() => ({
        innerWidth: window.innerWidth,
        documentWidth: document.documentElement.scrollWidth,
        bodyWidth: document.body.scrollWidth,
      }));
      expect(overflow.documentWidth).toBeLessThanOrEqual(overflow.innerWidth);
      expect(overflow.bodyWidth).toBeLessThanOrEqual(overflow.innerWidth);
      expect(consoleErrors).toEqual([]);
      expect(pageErrors).toEqual([]);
      expect(unknownRequests).toEqual([]);

      if ((locale === "zh-CN" && width === 1280) || (locale === "en" && width === 1920)) {
        await page.screenshot({
          path: testInfo.outputPath(`system-status-${locale}-${width}.png`),
          fullPage: true,
        });
      }
    });
  }
}

test("Issue #130 System Status refreshes normal, warning, and error facts", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const runtime = { mode: "normal" as RuntimeMode, healthCalls: 0, workerCalls: 0 };
  const unknownRequests = await installRoutes(page, "zh-CN", runtime);
  await seedAuthenticatedLocale(page, "zh-CN");
  await page.goto("/");
  await expect(page.getByTestId("system-status-summary")).toHaveText("系统正常");
  const initialHealthCalls = runtime.healthCalls;
  const initialWorkerCalls = runtime.workerCalls;
  await page.getByTestId("system-status-summary").click();

  runtime.mode = "warning";
  await page.getByTestId("system-status-refresh").click();
  await expect(page.getByTestId("system-status-summary")).toHaveText("系统预警");
  await expect(page.getByTestId("system-status-summary").locator(".ant-badge-status-warning")).toBeVisible();
  await expect(page.getByText("1/2 个运行节点可执行 v3 任务")).toBeVisible();

  runtime.mode = "error";
  await page.getByTestId("system-status-refresh").click();
  await expect(page.getByTestId("system-status-summary")).toHaveText("系统异常");
  await expect(page.getByTestId("system-status-summary").locator(".ant-badge-status-error")).toBeVisible();
  expect(runtime.healthCalls).toBe(initialHealthCalls + 2);
  expect(runtime.workerCalls).toBe(initialWorkerCalls + 2);
  expect(unknownRequests).toEqual([]);
});
