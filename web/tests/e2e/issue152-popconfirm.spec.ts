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

const outputEndMarker = "OUTPUT_END_VISIBLE_8d261";
const longOutput = {
  rows: Array.from({ length: 80 }, (_, index) => ({
    item: index,
    value: `output-scroll-line-${String(index).padStart(3, "0")}`,
  })),
  final_marker: outputEndMarker,
};

const completedExecution = {
  ...execution,
  status: "succeeded",
  output: longOutput,
  started_at: "2026-09-19T00:00:01Z",
  ended_at: "2026-09-19T00:00:02Z",
  duration_ms: 1000,
};

function completedAttempts() {
  return [1, 2].map((attemptNo) => ({
    id: 800 + attemptNo,
    execution_id: 71,
    adapter_id: 1,
    attempt_no: attemptNo,
    worker_id: 1,
    fencing_token: attemptNo,
    lease_expires_at: "2026-09-19T00:00:20Z",
    status: attemptNo === 2 ? "succeeded" : "worker_lost",
    claimed_at: "2026-09-19T00:00:01Z",
    started_at: "2026-09-19T00:00:01Z",
    ended_at: "2026-09-19T00:00:02Z",
    error_code: attemptNo === 2 ? null : "worker_lost",
    resource_usage_json: null,
    output_summary: null,
    cleanup_summary: { workspace_cleanup_status: "completed" },
  }));
}

async function installCompletedExecutionRoutes(page: Page) {
  const tracker = await installRoutes(page);
  await page.route("**/api/executions/71", (route) => fulfillJson(route, completedExecution));
  await page.route("**/api/adapters/1/executions*", (route) => fulfillJson(route, {
    items: [{ ...executionSummary, status: "succeeded" }],
    next_before_id: null,
  }));
  await page.route("**/api/executions/71/reliable-detail", (route) => fulfillJson(route, {
    execution_id: 71,
    dispatch_backend: "rabbitmq",
    status: "succeeded",
    attempts: completedAttempts(),
    incidents: [
      {
        ...incident,
        id: 900,
        status: "resolved",
        disposition_count: 1,
        recovery_dispatch_count: 1,
        recover_available: false,
        recover_reason: "incident_closed",
        terminate_available: false,
        terminate_reason: "incident_closed",
        recent_disposition: {
          id: "fixture-receipt-900",
          outcome: "recovery_dispatched",
          code: "recovery_dispatched",
          from_generation: 1,
          to_generation: 2,
        },
      },
      {
        ...incident,
        id: 901,
        status: "resolved",
        disposition_count: 1,
        recover_available: false,
        recover_reason: "incident_closed",
        terminate_available: false,
        terminate_reason: "incident_closed",
        recent_disposition: {
          id: "fixture-receipt-901",
          outcome: "execution_cancelled",
          code: "execution_cancelled",
          from_generation: 2,
          to_generation: null,
        },
      },
    ],
    replay_available: false,
    replay_reason: "execution_terminal",
  }));
  return tracker;
}

async function markerIsFullyVisible(page: Page) {
  return page.getByTestId("output-content").evaluate((element, marker) => {
    const content = element.textContent ?? "";
    const offset = content.indexOf(marker);
    const text = element.firstChild;
    if (offset < 0 || text === null) return false;

    const range = document.createRange();
    range.setStart(text, offset);
    range.setEnd(text, offset + marker.length);
    const markerRect = range.getBoundingClientRect();
    if (markerRect.top < 0 || markerRect.bottom > window.innerHeight) return false;

    let ancestor = element.parentElement;
    while (ancestor !== null) {
      const overflowY = getComputedStyle(ancestor).overflowY;
      if (["auto", "scroll", "hidden", "clip"].includes(overflowY)) {
        const ancestorRect = ancestor.getBoundingClientRect();
        if (markerRect.top < ancestorRect.top || markerRect.bottom > ancestorRect.bottom) return false;
      }
      ancestor = ancestor.parentElement;
    }
    return true;
  }, outputEndMarker);
}

for (const viewport of [{ width: 1306, height: 768 }, { width: 390, height: 844 }]) {
  test(`native wheel reaches long output end at ${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const { unknownRequests, dispositionRequests } = await installCompletedExecutionRoutes(page);
    await openIncidentHistory(page);
    await expect.poll(() => page.locator(".ant-drawer").evaluateAll((drawers) => (
      drawers.flatMap((drawer) => drawer.getAnimations({ subtree: true }))
        .filter((animation) => animation.playState === "running").length
    ))).toBe(0);

    const drawerBody = page.locator(".execution-history-drawer .ant-drawer-body");
    const outputTab = page.getByRole("dialog").getByRole("tab", { name: "输出", exact: true });
    const bodyBox = await drawerBody.boundingBox();
    expect(bodyBox).not.toBeNull();
    await page.mouse.move(bodyBox!.x + bodyBox!.width / 2, bodyBox!.y + bodyBox!.height / 2);
    for (let index = 0; index < 4; index += 1) await page.mouse.wheel(0, 1200);
    await expect.poll(() => drawerBody.evaluate((body) => body.scrollTop)).toBeGreaterThan(0);
    await expect(outputTab).toBeInViewport();
    await outputTab.click();

    const output = page.getByTestId("output-content");
    await expect(output).toContainText(outputEndMarker);
    expect(await markerIsFullyVisible(page)).toBe(false);
    const outputBox = await output.boundingBox();
    expect(outputBox).not.toBeNull();
    const wheelY = Math.min(
      bodyBox!.y + bodyBox!.height - 24,
      Math.max(bodyBox!.y + 24, outputBox!.y + 24),
    );
    await page.mouse.move(bodyBox!.x + bodyBox!.width / 2, wheelY);
    for (let index = 0; index < 8; index += 1) await page.mouse.wheel(0, 2400);
    await expect.poll(() => markerIsFullyVisible(page)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath("output-end-visible.png") });

    expect(dispositionRequests).toHaveLength(0);
    expect(unknownRequests).toEqual([]);
  });
}
