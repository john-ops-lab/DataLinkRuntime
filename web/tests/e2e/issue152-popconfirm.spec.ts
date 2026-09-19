import { expect, test, type Page, type Route } from "@playwright/test";

const adapter = {
  id: 1,
  name: "Incident disposition fixture",
  description: "Browser-only fixture for the real Ant Design Popconfirm event chain.",
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
  access_level: "admin",
  created_at: "2026-09-19T00:00:00Z",
  updated_at: "2026-09-19T00:00:00Z",
};

const version = {
  id: 10,
  adapter_id: 1,
  seq: 7,
  created_at: "2026-09-19T00:00:00Z",
};

const execution = {
  dispatch_backend: "rabbitmq",
  id: 71,
  adapter_id: 1,
  version_id: 10,
  worker_id: 1,
  target_worker_id: 1,
  trigger: "manual",
  scheduled_for: null,
  status: "queued",
  dispatch_generation: 2,
  input: null,
  input_source_type: "none",
  input_config_revision: 4,
  input_snapshot: { source_type: "none", revision: 4 },
  output: null,
  output_size: null,
  output_truncated: false,
  output_preview: null,
  stdout: "",
  stdout_truncated: false,
  stderr: "",
  stderr_truncated: false,
  error: null,
  error_code: null,
  locale: "zh-CN",
  created_at: "2026-09-19T00:00:00Z",
  started_at: null,
  ended_at: null,
  duration_ms: null,
};

const executionSummary = {
  id: 71,
  adapter_id: 1,
  version_id: 10,
  version_seq: 7,
  worker_id: 1,
  worker_name: "fixture-worker",
  trigger: "manual",
  scheduled_for: null,
  status: "queued",
  created_at: "2026-09-19T00:00:00Z",
  started_at: null,
  ended_at: null,
  duration_ms: null,
};

const incident = {
  id: 901,
  execution_id: 71,
  dispatch_generation: 2,
  message_id: "dispatch-71-2",
  kind: "delivery_limit",
  status: "open",
  attempts: 2,
  observation_count: 3,
  disposition_count: 0,
  recovery_dispatch_count: 0,
  last_error: "delivery_limit",
  created_at: "2026-09-19T00:00:04Z",
  resolved_at: null,
  recent_disposition: null,
  dispositions_url: "/api/executions/71/incidents/901/dispositions",
  recover_available: true,
  recover_reason: null,
  terminate_available: true,
  terminate_reason: null,
};

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function installRoutes(page: Page) {
  const unknownRequests: string[] = [];
  const dispositionRequests: Array<{ body: unknown; idempotencyKey: string | null }> = [];

  await page.route("**/entry-mode.js", (route) => route.fulfill({
    contentType: "application/javascript",
    body: "window.__DLR_ENTRY_MODE__ = 'token';",
  }));
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;

    if (method === "GET" && path === "/api/locale") return fulfillJson(route, { locale: "zh-CN" });
    if (method === "GET" && path === "/api/auth/admin/verify") return fulfillJson(route, { status: "ok" });
    if (method === "GET" && path === "/api/health") return fulfillJson(route, { status: "ok", database: true });
    if (method === "GET" && path === "/api/workers") return fulfillJson(route, [{
      id: 1,
      name: "fixture-worker",
      status: "online",
      last_heartbeat: "2026-09-19T00:00:00Z",
      capabilities: ["python"],
    }]);
    if (method === "GET" && path === "/api/system/managed-input-capability") return fulfillJson(route, {
      managed_files_enabled: false,
      ready: false,
      default_retention_seconds: 86_400,
      max_custom_retention_seconds: 2_592_000,
      allow_manual_delete: true,
      allowed_extensions: [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"],
    });
    if (method === "GET" && path === "/api/adapters") return fulfillJson(route, [adapter]);
    if (method === "GET" && path === "/api/adapters/1") return fulfillJson(route, adapter);
    if (method === "GET" && path === "/api/adapters/1/versions") return fulfillJson(route, [version]);
    if (method === "GET" && path === "/api/adapters/1/versions/10") return fulfillJson(route, {
      ...version,
      code: "def transform(payload):\n    return payload\n",
      requirements: "",
      runtime_config: {},
    });
    if (method === "GET" && path === "/api/adapters/1/schedule") return fulfillJson(route, {
      detail: { code: "schedule_not_configured", message: "Fixture schedule is not configured" },
    }, 404);
    if (method === "GET" && path === "/api/adapters/1/credential-bindings") return fulfillJson(route, []);
    if (method === "GET" && path === "/api/adapters/1/credential-options") return fulfillJson(route, []);
    if (method === "GET" && path === "/api/adapters/1/input-config") return fulfillJson(route, {
      adapter_id: 1,
      revision: 4,
      source_type: "none",
      json_value: null,
      retention: { mode: "system_default", seconds: null },
      artifacts: [],
      valid_for_run: true,
      invalid_reason: null,
    });
    if (method === "GET" && path === "/api/ai/attachment-capabilities") return fulfillJson(route, {
      limits: { max_attachments: 5, max_file_bytes: 2_000_000, max_total_bytes: 5_000_000 },
      supported_content_types: ["text/plain", "application/json"],
    });
    if (method === "GET" && path === "/api/adapters/1/ai/knowledge-capability") return fulfillJson(route, {
      available: false,
      reason: "fixture_knowledge_source_disabled",
    });
    if (method === "GET" && path === "/api/adapters/1/executions") return fulfillJson(route, {
      items: [executionSummary],
      next_before_id: null,
    });
    if (method === "GET" && path === "/api/executions/71") return fulfillJson(route, execution);
    if (method === "GET" && path === "/api/executions/71/events") return route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "cache-control": "no-cache" },
      body: `event: execution\ndata: ${JSON.stringify(execution)}\n\n`,
    });
    if (method === "GET" && path === "/api/executions/71/reliable-detail") return fulfillJson(route, {
      execution_id: 71,
      dispatch_backend: "rabbitmq",
      status: "queued",
      attempts: [],
      incidents: [incident],
      replay_available: false,
      replay_reason: null,
    });
    if (method === "POST" && path === "/api/executions/71/incidents/901/dispositions") {
      const body = request.postDataJSON() as {
        action: "recover" | "terminate";
        expected_generation: number;
        reason_code: "capacity_repaired" | "operator_cancel";
      };
      dispositionRequests.push({
        body,
        idempotencyKey: request.headers()["idempotency-key"] ?? null,
      });
      return fulfillJson(route, {
        receipt: {
          id: "receipt-901",
          incident_id: 901,
          execution_id: 71,
          idempotency_key: request.headers()["idempotency-key"],
          actor_kind: "admin_token",
          user_id: null,
          action: body.action,
          reason_code: body.reason_code,
          outcome: body.action === "recover" ? "recovery_dispatched" : "execution_cancelled",
          code: body.action === "recover" ? "recovery_dispatched" : "execution_cancelled",
          from_generation: 2,
          to_generation: body.action === "recover" ? 3 : null,
          from_outbox_id: "outbox-2",
          to_outbox_id: body.action === "recover" ? "outbox-3" : null,
          execution_status: body.action === "recover" ? "queued" : "cancelled",
          created_at: "2026-09-19T00:01:00Z",
        },
        incident_status: "resolved",
        execution_status: body.action === "recover" ? "queued" : "cancelled",
        retry_after_seconds: null,
      });
    }

    unknownRequests.push(`${method} ${path}${url.search}`);
    return fulfillJson(route, {
      detail: { code: "unhandled_test_request", message: `${method} ${path}` },
    }, 404);
  });

  return { unknownRequests, dispositionRequests };
}

async function openIncidentHistory(page: Page) {
  await page.goto("/");
  await page.getByTestId("admin-token-input").fill("fixture-token");
  await page.getByTestId("admin-token-submit").click();
  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
  await page.getByTestId("adapter-item").click();
  await expect(page.getByTestId("workbench-header")).toBeVisible();
  await page.getByRole("tab", { name: "执行记录" }).click();
  await page.getByTestId("history-row").click();
  await expect(page.getByRole("dialog")).toBeVisible();
}

async function tabToButton(page: Page, name: RegExp, limit = 12) {
  const button = page.getByRole("button", { name });
  const visited: string[] = [];
  for (let index = 0; index < limit; index += 1) {
    await page.keyboard.press("Tab");
    if (await button.evaluate((element) => element === document.activeElement)) {
      return;
    }
    visited.push(await page.evaluate(() => {
      const active = document.activeElement;
      if (!(active instanceof HTMLElement)) return "none";
      return active.getAttribute("aria-label")
        ?? active.textContent?.replace(/\s+/g, " ").trim()
        ?? active.tagName;
    }));
  }
  throw new Error(`Popconfirm button was not keyboard reachable; visited: ${visited.join(" -> ")}`);
}

test("real Ant Design recover Popconfirm cancels without writing and confirms exactly once", async ({ page }) => {
  const { unknownRequests, dispositionRequests } = await installRoutes(page);
  await openIncidentHistory(page);

  await page.getByRole("button", { name: "恢复此执行" }).click();
  await expect(page.getByText("确认恢复此执行？")).toBeVisible();
  await expect(page.getByText("将按冻结的版本、输入、目标和资源快照继续原执行。")).toBeVisible();
  expect(dispositionRequests).toHaveLength(0);
  await page.getByRole("button", { name: /取\s*消/ }).click();
  await expect(page.getByText("确认恢复此执行？")).toBeHidden();
  expect(dispositionRequests).toHaveLength(0);

  await page.getByRole("button", { name: "恢复此执行" }).click();
  await expect(page.getByText("确认恢复此执行？")).toBeVisible();
  await page.getByRole("button", { name: /确\s*认/ }).click();
  await expect.poll(() => dispositionRequests.length).toBe(1);
  expect(dispositionRequests[0]?.body).toEqual({
    action: "recover",
    expected_generation: 2,
    reason_code: "capacity_repaired",
  });
  expect(dispositionRequests[0]?.idempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
  expect(unknownRequests).toEqual([]);
});

test("real Ant Design terminate Popconfirm submits exactly once", async ({ page }) => {
  const { unknownRequests, dispositionRequests } = await installRoutes(page);
  await openIncidentHistory(page);

  await page.getByRole("button", { name: "终结此执行" }).click();
  await expect(page.getByText("确认终结此执行？")).toBeVisible();
  await expect(page.getByText("将按当前状态机终结或请求取消原执行。")).toBeVisible();
  expect(dispositionRequests).toHaveLength(0);
  await page.getByRole("button", { name: /确\s*认/ }).click();
  await expect.poll(() => dispositionRequests.length).toBe(1);
  expect(dispositionRequests[0]?.body).toEqual({
    action: "terminate",
    expected_generation: 2,
    reason_code: "operator_cancel",
  });
  expect(dispositionRequests[0]?.idempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
  expect(unknownRequests).toEqual([]);
});

test("recover Popconfirm keeps cancel and confirm inside the Drawer keyboard cycle", async ({ page }) => {
  const { unknownRequests, dispositionRequests } = await installRoutes(page);
  await openIncidentHistory(page);

  const recover = page.getByRole("button", { name: "恢复此执行" });
  await recover.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("确认恢复此执行？")).toBeVisible();
  await tabToButton(page, /取\s*消/);
  await page.keyboard.press("Enter");
  await expect(page.getByText("确认恢复此执行？")).toBeHidden();
  expect(dispositionRequests).toHaveLength(0);

  await recover.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("确认恢复此执行？")).toBeVisible();
  await tabToButton(page, /确\s*认/);
  await page.keyboard.press("Enter");
  await expect.poll(() => dispositionRequests.length).toBe(1);
  expect(dispositionRequests[0]?.body).toEqual({
    action: "recover",
    expected_generation: 2,
    reason_code: "capacity_repaired",
  });
  expect(unknownRequests).toEqual([]);
});

test("terminate Popconfirm confirm stays inside the Drawer keyboard cycle", async ({ page }) => {
  const { unknownRequests, dispositionRequests } = await installRoutes(page);
  await openIncidentHistory(page);

  const terminate = page.getByRole("button", { name: "终结此执行" });
  await terminate.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("确认终结此执行？")).toBeVisible();
  await tabToButton(page, /确\s*认/);
  await page.keyboard.press("Enter");
  await expect.poll(() => dispositionRequests.length).toBe(1);
  expect(dispositionRequests[0]?.body).toEqual({
    action: "terminate",
    expected_generation: 2,
    reason_code: "operator_cancel",
  });
  expect(unknownRequests).toEqual([]);
});
