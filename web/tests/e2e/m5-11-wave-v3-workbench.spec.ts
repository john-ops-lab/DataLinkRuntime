import { Buffer } from "node:buffer";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page, type Route } from "@playwright/test";

type Locale = "zh-CN" | "en";
type Scenario = "admin" | "read" | "error";

const LOCALES: readonly Locale[] = ["zh-CN", "en"];
const VIEWPORTS = [1280, 1440, 1680, 1920] as const;
const specDir = dirname(fileURLToPath(import.meta.url));
const evidenceRoot = resolve(
  specDir,
  process.env.DLR_WAVE_D_OUTPUT_DIR ?? "../../../docs/ui/m5-11-wave-v3-workbench",
);
const screenshotDir = resolve(evidenceRoot, "browser");

const adapterOne = {
  id: 1,
  name: "订单同步适配器 Orders Synchronization Adapter — Long Label",
  description: "V3 fixture with long Chinese and English copy for responsive Workbench checks.",
  language: "python",
  adapter_type: "task",
  run_mode: "manual",
  timeout_seconds: 300,
  owner_user_id: 42,
  owner_username: "owner-with-a-long-display-name",
  latest_version_id: 10,
  runtime_worker_id: 1,
  runtime_locked: false,
  archived_at: null,
  running_execution_id: null as number | null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const adapterTwo = {
  ...adapterOne,
  id: 2,
  name: "第二个隔离适配器 Second Isolated Adapter",
  description: "A separate fixture Adapter used only to verify isolation on switch.",
  latest_version_id: 20,
};

const worker = {
  id: 1,
  name: "runtime-worker-with-an-intentionally-long-display-name",
  status: "online",
  last_heartbeat: "2026-01-01T00:00:00Z",
  capabilities: ["python", "javascript", "java"],
};

const versionOne = {
  id: 10,
  adapter_id: 1,
  seq: 7,
  created_at: "2026-01-01T00:00:00Z",
};

const versionTwo = {
  id: 20,
  adapter_id: 2,
  seq: 3,
  created_at: "2026-01-01T00:00:00Z",
};

const execution = {
  id: 5,
  adapter_id: 1,
  version_id: 10,
  worker_id: 1,
  target_worker_id: 1,
  trigger: "manual",
  scheduled_for: null,
  status: "running" as const,
  input: { fixture: true, message: "deterministic browser input" },
  output: null,
  output_size: null,
  output_truncated: false,
  output_preview: null,
  stdout: "fixture stdout line 1\nfixture stdout line 2\n这是浏览器可见的已脱敏日志，包含一段需要在终端内部折行而不能撑宽页面的长文本：request_id=fixture-2026-01-01-very-long-observation-line-without-sensitive-values-and-with-repeated-context-for-overflow-checks\n",
  stdout_truncated: false,
  stderr: "",
  stderr_truncated: false,
  error: null,
  locale: "zh-CN" as const,
  created_at: "2026-01-01T00:00:00Z",
  started_at: "2026-01-01T00:00:01Z",
  ended_at: null,
  duration_ms: null,
};

const completedExecution = {
  ...execution,
  status: "succeeded" as const,
  output: { ok: true, fixture: "deterministic" },
  output_size: 44,
  ended_at: "2026-01-01T00:00:03Z",
  duration_ms: 2000,
};

const executionSummary = {
  id: execution.id,
  adapter_id: execution.adapter_id,
  version_id: execution.version_id,
  version_seq: versionOne.seq,
  worker_id: execution.worker_id,
  worker_name: worker.name,
  trigger: execution.trigger,
  scheduled_for: null,
  status: "succeeded" as const,
  created_at: execution.created_at,
  started_at: execution.started_at,
  ended_at: completedExecution.ended_at,
  duration_ms: completedExecution.duration_ms,
};

const attachmentCapabilities = {
  limits: {
    max_attachments: 5,
    max_file_bytes: 2_000_000,
    max_total_bytes: 5_000_000,
  },
  supported_content_types: [
    "text/plain",
    "text/markdown",
    "text/x-python",
    "application/json",
  ],
};

interface BrowserRecord {
  locale: Locale;
  width: number;
  scenario: Scenario;
  screenshot: string;
  console_errors: string[];
  expected_console_errors: string[];
  page_errors: string[];
  http_errors: Array<{ path: string; status: number }>;
  unexpected_http_errors: Array<{ path: string; status: number }>;
  unknown_requests: string[];
  overflow: {
    inner_width: number;
    document_scroll_width: number;
    body_scroll_width: number;
  };
  visible_states: Record<string, boolean>;
  assist_request_count: number;
}

const records: BrowserRecord[] = [];
let browserVersion = "unknown";

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

function versionFor(adapterId: number) {
  return adapterId === 1 ? versionOne : versionTwo;
}

function detailFor(adapterId: number) {
  const version = versionFor(adapterId);
  return {
    ...version,
    code: adapterId === 1
      ? "def transform(payload):\n    return {'fixture': True, 'payload': payload}\n"
      : "def isolated(payload):\n    return payload\n",
    requirements: "",
    runtime_config: {},
  };
}

function adapterFor(adapterId: number, accessLevel: "admin" | "read") {
  return {
    ...(adapterId === 1 ? adapterOne : adapterTwo),
    access_level: accessLevel,
  };
}

function diagnosticsFor(page: Page, scenario: Scenario) {
  const consoleErrors: string[] = [];
  const expectedConsoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() !== "error") {
      return;
    }
    const text = message.text();
    const expected =
      scenario === "read" && text.includes("status of 401") ||
      scenario === "error" && text.includes("status of 503");
    if (expected) {
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
  scenario: Scenario,
  locale: Locale,
): Promise<{ unknownRequests: string[]; assistRequests: unknown[] }> {
  let accountLoggedIn = false;
  const assistRequests: unknown[] = [];
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
      await fulfillJson(route, [
        adapterFor(1, scenario === "read" ? "read" : "admin"),
        adapterFor(2, scenario === "read" ? "read" : "admin"),
      ]);
      return;
    }
    const adapterMatch = path.match(/^\/api\/adapters\/(\d+)$/);
    if (adapterMatch && method === "GET") {
      await fulfillJson(route, adapterFor(Number(adapterMatch[1]), scenario === "read" ? "read" : "admin"));
      return;
    }
    const versionsMatch = path.match(/^\/api\/adapters\/(\d+)\/versions$/);
    if (versionsMatch && method === "GET") {
      const adapterId = Number(versionsMatch[1]);
      await fulfillJson(route, [versionFor(adapterId)]);
      return;
    }
    const versionDetailMatch = path.match(/^\/api\/adapters\/(\d+)\/versions\/(\d+)$/);
    if (versionDetailMatch && method === "GET") {
      await fulfillJson(route, detailFor(Number(versionDetailMatch[1])));
      return;
    }
    const scheduleMatch = path.match(/^\/api\/adapters\/(\d+)\/schedule$/);
    if (scheduleMatch && method === "GET") {
      await fulfillJson(route, errorBody("schedule_not_configured", "Fixture schedule is not configured"), 404);
      return;
    }
    const bindingsMatch = path.match(/^\/api\/adapters\/(\d+)\/credential-bindings$/);
    if (bindingsMatch && method === "GET") {
      await fulfillJson(route, []);
      return;
    }
    const optionsMatch = path.match(/^\/api\/adapters\/(\d+)\/credential-options$/);
    if (optionsMatch && method === "GET") {
      await fulfillJson(route, []);
      return;
    }
    if (path === "/api/ai/attachment-capabilities" && method === "GET") {
      await fulfillJson(route, attachmentCapabilities);
      return;
    }
    const knowledgeCapabilityMatch = path.match(/^\/api\/adapters\/(\d+)\/ai\/knowledge-capability$/);
    if (knowledgeCapabilityMatch && method === "GET") {
      await fulfillJson(route, { available: false, reason: "fixture_knowledge_source_disabled" });
      return;
    }
    if (path === "/api/auth/admin/verify" && method === "GET" && scenario !== "read") {
      await fulfillJson(route, { status: "ok" });
      return;
    }
    if (path === "/api/auth/account/csrf" && method === "GET" && scenario === "read") {
      await fulfillJson(route, { status: "ok" }, 200, {
        "set-cookie": "dlr_account_csrf=wave-d-csrf; Path=/",
      });
      return;
    }
    if (path === "/api/auth/account/me" && method === "GET" && scenario === "read") {
      if (accountLoggedIn) {
        await fulfillJson(route, {
          principal: {
            id: 7,
            username: "reader-user-with-a-long-display-name",
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
          username: "reader-user-with-a-long-display-name",
          role: "user",
          enabled: true,
          must_change_password: false,
        },
      });
      return;
    }
    if (path === "/api/adapters/1/executions" && method === "POST" && scenario === "admin") {
      await fulfillJson(route, execution, 201);
      return;
    }
    if (path === "/api/executions/5" && method === "GET" && scenario === "admin") {
      await fulfillJson(route, completedExecution);
      return;
    }
    if (path === "/api/executions/5/events" && method === "GET" && scenario === "admin") {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "cache-control": "no-cache" },
        body: `event: execution\ndata: ${jsonBody(completedExecution)}\n\n`,
      });
      return;
    }
    const historyMatch = path.match(/^\/api\/adapters\/(\d+)\/executions$/);
    if (historyMatch && method === "GET" && scenario === "admin") {
      await fulfillJson(route, Number(historyMatch[1]) === 1
        ? { items: [executionSummary], next_before_id: null }
        : { items: [], next_before_id: null });
      return;
    }
    if (path === "/api/adapters/1/ai/assist" && method === "POST" && scenario === "admin") {
      assistRequests.push(request.postDataJSON());
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 300));
      await fulfillJson(route, {
        message: "# Fixture response\n\nThe candidate is deterministic and safe to review.\n\n```python\nreturn {'fixture': True}\n```",
        provider: "fixture",
        model: "fixture-model",
        candidate: {
          summary: "Fixture candidate: update the transform code",
          code: "def transform(payload):\n    return {'fixture': True, 'payload': payload}\n",
          required_secret_keys: [],
        },
        tool_calls: [{
          tool_name: "search_knowledge",
          status: "success",
          args_summary: "fixture query",
          result_summary: "fixture result",
          error_code: null,
        }],
      });
      return;
    }
    if (path === "/api/adapters/1/ai/assist" && method === "POST" && scenario === "error") {
      assistRequests.push(request.postDataJSON());
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 300));
      await fulfillJson(route, errorBody("ai_provider_unavailable", "Fixture AI provider is unavailable"), 503);
      return;
    }

    const requestKey = `${method} ${path}${url.search}`;
    unknownRequests.push(requestKey);
    await fulfillJson(route, errorBody("wave_d_unhandled_request", requestKey), 404);
  });

  return { unknownRequests, assistRequests };
}

async function screenshot(page: Page, name: string): Promise<string> {
  mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({
    path: resolve(screenshotDir, name),
    fullPage: true,
    animations: "disabled",
  });
  return `docs/ui/m5-11-wave-v3-workbench/browser/${name}`;
}

async function waitForCandidateDiff(page: Page, locale: Locale) {
  const title = locale === "zh-CN" ? "AI 候选修改：与当前编辑内容对比" : "AI candidate changes: compare with current code";
  const originalTitle = locale === "zh-CN" ? "当前编辑内容" : "Current code";
  const modifiedTitle = locale === "zh-CN" ? "AI 候选修改" : "AI candidate changes";
  const diffRegion = page.getByTestId("version-diff");
  const modalContent = page.locator(".ant-modal-wrap:visible .ant-modal-content");
  const diffTitles = diffRegion.locator(".diff-modal-titles > span");
  const diffEditor = diffRegion.locator(".monaco-diff-editor");

  await expect(modalContent).toBeVisible();
  await expect(diffRegion).toBeVisible();
  await expect(diffRegion).toHaveAccessibleName(title);
  await expect(diffTitles).toHaveCount(2);
  await expect(diffTitles.nth(0)).toHaveText(originalTitle);
  await expect(diffTitles.nth(1)).toHaveText(modifiedTitle);
  await expect(diffEditor).toBeVisible();
  await expect(page.getByTestId("diff-apply-candidate")).toBeVisible();
  await expect(page.getByTestId("diff-apply-candidate")).toBeEnabled();
  await expect(page.getByTestId("diff-close")).toBeVisible();

  // Require two identical visible geometry/style snapshots instead of a blind
  // delay: Ant Design's modal transition and Monaco's first layout must both
  // settle before the evidence screenshot is captured.
  let previousSnapshot = "";
  await expect.poll(
    async () => {
      const snapshot = await page.evaluate(() => {
        const rectFor = (element: Element | null) => {
          if (element === null) return null;
          const rect = element.getBoundingClientRect();
          return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
        };
        const diff = document.querySelector('[data-testid="version-diff"]');
        const modal = diff?.closest(".ant-modal-content") ?? null;
        const editor = diff?.querySelector(".monaco-diff-editor") ?? null;
        const modalStyle = modal === null ? null : getComputedStyle(modal);
        const diffStyle = diff === null ? null : getComputedStyle(diff);
        return {
          modal: rectFor(modal),
          diff: rectFor(diff),
          editor: rectFor(editor),
          modalOpacity: modalStyle?.opacity ?? "",
          modalVisibility: modalStyle?.visibility ?? "",
          diffOpacity: diffStyle?.opacity ?? "",
          diffVisibility: diffStyle?.visibility ?? "",
        };
      });
      const serialized = JSON.stringify(snapshot);
      const stable = serialized === previousSnapshot &&
        snapshot.modal !== null && snapshot.modal.width > 0 && snapshot.modal.height > 0 &&
        snapshot.diff !== null && snapshot.diff.width > 0 && snapshot.diff.height > 0 &&
        snapshot.editor !== null && snapshot.editor.width > 0 && snapshot.editor.height > 0 &&
        snapshot.modalOpacity === "1" && snapshot.modalVisibility === "visible" &&
        snapshot.diffOpacity === "1" && snapshot.diffVisibility === "visible";
      previousSnapshot = serialized;
      return stable;
    },
    { timeout: 10_000, intervals: [100, 250, 500] },
  ).toBe(true);
}

async function measureOverflow(page: Page) {
  return page.evaluate(() => ({
    inner_width: window.innerWidth,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
  }));
}

async function loginAndOpenAdapter(page: Page, locale: Locale, scenario: Scenario, adapterId = 1) {
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
  await page.getByTestId("adapter-item").nth(adapterId - 1).click();
  await expect(page.getByTestId("workbench-header")).toBeVisible();
  await expect(page.getByTestId("editor-main")).toBeVisible();
}

async function runAdminScenario(page: Page, locale: Locale, width: number, diagnostics: ReturnType<typeof diagnosticsFor>, unknownRequests: string[], assistRequests: unknown[]) {
  const visibleStates: Record<string, boolean> = {};
  await loginAndOpenAdapter(page, locale, "admin");
  visibleStates.workbench = await page.getByTestId("workbench-header").isVisible();
  visibleStates.workbench_context = await page.getByTestId("workbench-meta").isVisible();
  visibleStates.workbench_toolbar = await page.getByTestId("workbench-toolbar").isVisible();
  await expect(page.getByTestId("workbench-toolbar")).toHaveAttribute(
    "aria-label",
    locale === "zh-CN" ? "任务操作" : "Task actions",
  );
  visibleStates.editor_region = await page.getByRole("region", { name: locale === "zh-CN" ? "适配器代码编辑器" : "Adapter code editor" }).isVisible();
  visibleStates.editor_toolbar = await page.getByRole("toolbar", { name: locale === "zh-CN" ? "代码编辑器工具栏" : "Code editor toolbar" }).isVisible();
  await expect(page.getByTestId("working-diff")).toHaveAccessibleName(locale === "zh-CN" ? "查看差异" : "View diff");
  visibleStates.header_icon_action = await page.getByTestId("adapter-settings").evaluate((element) => element.querySelector("svg") !== null);
  await screenshot(page, `${locale}-${width}-workbench.png`);

  await page.getByTestId("header-task-run-once").click();
  await expect(page.getByRole("tab", { name: locale === "zh-CN" ? "实时日志" : "Live logs" })).toBeVisible();
  const liveTab = page.getByRole("tab", { name: locale === "zh-CN" ? "实时日志" : "Live logs" });
  await expect(liveTab).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("live-log")).toBeVisible();
  visibleStates.live_log = await page.getByTestId("live-log").isVisible();
  await page.getByTestId("live-log-pause").click();
  await expect(page.getByTestId("live-log-resume")).toBeVisible();
  visibleStates.live_log_paused = true;
  await page.getByTestId("live-log-resume").click();
  await expect(page.getByTestId("live-log-pause")).toBeVisible();
  visibleStates.live_log_follow_resumed = true;
  const liveLog = page.getByTestId("live-log");
  await liveLog.selectText();
  const logContext = page.getByTestId("live-log-add-context");
  await expect(logContext).toBeEnabled();
  await logContext.click();
  visibleStates.log_context = await page.getByTestId("ai-context-snippets").isVisible();
  await page.getByTestId("live-log-maximize").click();
  await expect(page.getByTestId("live-log-restore")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("live-log-maximize")).toBeVisible();
  visibleStates.live_log_keyboard_restore = true;
  await screenshot(page, `${locale}-${width}-logs.png`);

  await page.getByTestId("close-ai-assistant").click();
  await page.getByRole("tab", { name: locale === "zh-CN" ? "执行记录" : "Executions" }).click();
  visibleStates.history_toolbar = await page.getByTestId("history-toolbar").isVisible();
  await expect(page.getByTestId("history-row")).toBeVisible();
  await page.getByTestId("history-row").click();
  await expect(page.getByRole("dialog")).toBeVisible();
  visibleStates.history_detail = await page.getByRole("dialog").isVisible();
  await page.getByRole("dialog").getByRole("tab", { name: locale === "zh-CN" ? "执行日志" : "Execution log" }).click();
  await expect(page.getByTestId("detail-log")).toBeVisible();
  await page.getByTestId("detail-log-maximize").click();
  await expect(page.getByTestId("detail-log-restore")).toBeVisible();
  await page.getByTestId("detail-log-restore").click();
  visibleStates.history_log_restore = await page.getByTestId("detail-log-maximize").isVisible();
  await screenshot(page, `${locale}-${width}-history-detail.png`);
  await page.getByRole("dialog").getByRole("button", { name: locale === "zh-CN" ? "关闭" : "Close" }).click().catch(() => undefined);

  await page.getByRole("tab", { name: locale === "zh-CN" ? "编辑" : "Edit" }).click();
  await expect(page.getByTestId("open-ai-assistant")).toBeVisible();
  await expect(page.getByTestId("open-ai-assistant").locator("svg")).toBeVisible();
  visibleStates.ai_icon_action = true;
  await page.getByTestId("open-ai-assistant").click();
  await expect(page.getByTestId("ai-conversation-empty")).toBeVisible();
  await expect(page.getByTestId("ai-composer-actions")).toHaveAttribute(
    "aria-label",
    locale === "zh-CN" ? "AI 消息操作" : "AI message controls",
  );
  const input = page.getByTestId("ai-message-input");
  await input.fill("Please keep this unsent draft while the AI pane changes layout.");
  await page.getByTestId("maximize-ai-assistant").click();
  await expect(page.getByTestId("restore-ai-assistant")).toBeVisible();
  await expect(input).toHaveValue("Please keep this unsent draft while the AI pane changes layout.");
  await page.getByTestId("restore-ai-assistant").click();
  await expect(input).toHaveValue("Please keep this unsent draft while the AI pane changes layout.");
  visibleStates.ai_draft_preserved = true;
  await page.getByTestId("ai-attachment-input").setInputFiles({
    name: "fixture-attachment-with-a-very-long-name-for-zh-CN-and-en-context.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("deterministic attachment content", "utf8"),
  });
  await expect(page.getByTestId("ai-attachment-item")).toBeVisible();
  await input.fill("Generate a deterministic candidate from the selected context.");
  await page.getByTestId("ai-send").click();
  await expect(page.getByTestId("ai-loading")).toBeVisible();
  await expect(page.getByTestId("ai-message-assistant")).toBeVisible();
  await expect(page.getByTestId("ai-tool-call")).toBeVisible();
  await expect(page.getByTestId("ai-tool-status")).toContainText(locale === "zh-CN" ? "成功" : "Success");
  await expect(page.getByTestId("ai-candidate")).toBeVisible();
  await expect(page.getByTestId("ai-message-actions").first()).toBeVisible();
  await expect(page.getByTestId("ai-message-actions").first()).toHaveAttribute(
    "aria-label",
    locale === "zh-CN" ? "消息操作" : "Message actions",
  );
  await expect(page.getByTestId("ai-code-copy")).toBeVisible();
  visibleStates.ai_markdown_code = await page.getByTestId("ai-code-copy").isVisible();
  visibleStates.ai_tool_success = await page.getByTestId("ai-tool-call").isVisible();
  visibleStates.ai_candidate = await page.getByTestId("ai-candidate").isVisible();
  await screenshot(page, `${locale}-${width}-ai-candidate.png`);

  await page.getByTestId("ai-view-diff").click();
  await waitForCandidateDiff(page, locale);
  visibleStates.candidate_diff = await page.getByTestId("version-diff").isVisible();
  visibleStates.candidate_diff_modal_stable = true;
  await screenshot(page, `${locale}-${width}-candidate-diff.png`);
  await page.getByTestId("diff-apply-candidate").click();
  await expect(page.getByTestId("ai-candidate-applied")).toBeVisible();
  visibleStates.candidate_apply = await page.getByTestId("ai-candidate-applied").isVisible();

  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByTestId("adapter-item").nth(1).click();
  await expect(page.getByTestId("workbench-header")).toContainText("第二个隔离适配器");
  await expect(page.getByTestId("ai-conversation-empty")).toBeVisible();
  visibleStates.adapter_switch_isolated = await page.getByTestId("ai-conversation-empty").isVisible();
  await screenshot(page, `${locale}-${width}-adapter-switch.png`);

  const overflow = await measureOverflow(page);
  expect(overflow.document_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(overflow.body_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(unknownRequests).toEqual([]);
  expect(diagnostics.consoleErrors).toEqual([]);
  expect(diagnostics.pageErrors).toEqual([]);
  expect(assistRequests.length).toBe(1);
  expect((assistRequests[0] as { message?: string }).message).toContain("deterministic candidate");
  records.push({
    locale,
    width,
    scenario: "admin",
    screenshot: `docs/ui/m5-11-wave-v3-workbench/browser/${locale}-${width}-adapter-switch.png`,
    console_errors: diagnostics.consoleErrors,
    expected_console_errors: diagnostics.expectedConsoleErrors,
    page_errors: diagnostics.pageErrors,
    http_errors: [],
    unexpected_http_errors: [],
    unknown_requests: unknownRequests,
    overflow,
    visible_states: visibleStates,
    assist_request_count: assistRequests.length,
  });
}

async function runErrorScenario(page: Page, locale: Locale, width: number, diagnostics: ReturnType<typeof diagnosticsFor>, unknownRequests: string[], assistRequests: unknown[]) {
  await loginAndOpenAdapter(page, locale, "error");
  await page.getByTestId("open-ai-assistant").click();
  const input = page.getByTestId("ai-message-input");
  await input.fill("Trigger the deterministic error state.");
  await page.getByTestId("ai-send").click();
  await expect(page.getByTestId("ai-loading")).toBeVisible();
  await expect(page.getByTestId("ai-panel-error")).toBeVisible();
  const screenshotName = `${locale}-${width}-ai-error.png`;
  await screenshot(page, screenshotName);
  const overflow = await measureOverflow(page);
  expect(overflow.document_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(overflow.body_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(unknownRequests).toEqual([]);
  expect(diagnostics.consoleErrors).toEqual([]);
  expect(diagnostics.pageErrors).toEqual([]);
  expect(assistRequests.length).toBe(1);
  records.push({
    locale,
    width,
    scenario: "error",
    screenshot: `docs/ui/m5-11-wave-v3-workbench/browser/${screenshotName}`,
    console_errors: diagnostics.consoleErrors,
    expected_console_errors: diagnostics.expectedConsoleErrors,
    page_errors: diagnostics.pageErrors,
    http_errors: [],
    unexpected_http_errors: [],
    unknown_requests: unknownRequests,
    overflow,
    visible_states: { ai_loading: true, ai_error: true, request_completed: true },
    assist_request_count: assistRequests.length,
  });
}

async function runReadScenario(page: Page, locale: Locale, width: number, diagnostics: ReturnType<typeof diagnosticsFor>, unknownRequests: string[]) {
  await loginAndOpenAdapter(page, locale, "read");
  await expect(page.getByTestId("adapter-read-only-notice")).toBeVisible();
  await expect(page.getByTestId("adapter-read-only")).toBeVisible();
  await expect(page.getByTestId("editor-main")).toHaveAttribute("aria-label", locale === "zh-CN" ? "适配器代码编辑器" : "Adapter code editor");
  await expect(page.getByTestId("open-ai-assistant")).toHaveCount(0);
  const screenshotName = `${locale}-${width}-read-only.png`;
  await screenshot(page, screenshotName);
  const overflow = await measureOverflow(page);
  expect(overflow.document_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(overflow.body_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(unknownRequests).toEqual([]);
  expect(diagnostics.consoleErrors).toEqual([]);
  expect(diagnostics.pageErrors).toEqual([]);
  records.push({
    locale,
    width,
    scenario: "read",
    screenshot: `docs/ui/m5-11-wave-v3-workbench/browser/${screenshotName}`,
    console_errors: diagnostics.consoleErrors,
    expected_console_errors: diagnostics.expectedConsoleErrors,
    page_errors: diagnostics.pageErrors,
    http_errors: [{ path: "/api/auth/account/me", status: 401 }],
    unexpected_http_errors: [],
    unknown_requests: unknownRequests,
    overflow,
    visible_states: { read_only: true, ai_hidden: true },
    assist_request_count: 0,
  });
}

async function runCase(page: Page, locale: Locale, width: number, scenario: Scenario) {
  const { unknownRequests, assistRequests } = await installRoutes(page, scenario, locale);
  const diagnostics = diagnosticsFor(page, scenario);
  if (scenario === "admin") {
    await runAdminScenario(page, locale, width, diagnostics, unknownRequests, assistRequests);
  } else if (scenario === "error") {
    await runErrorScenario(page, locale, width, diagnostics, unknownRequests, assistRequests);
  } else {
    await runReadScenario(page, locale, width, diagnostics, unknownRequests);
  }
}

test.afterAll(() => {
  mkdirSync(evidenceRoot, { recursive: true });
  records.sort((left, right) =>
    left.locale.localeCompare(right.locale) || left.width - right.width || left.scenario.localeCompare(right.scenario),
  );
  writeFileSync(
    resolve(evidenceRoot, "browser-report.json"),
    `${JSON.stringify({
      schema_version: 1,
      product: "DataLinkRuntime",
      wave: "M5.11 V3",
      scope: "Workbench, runtime logs and AI spatial relationship visual/interaction convergence",
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
      fake_provider: "fixture",
      real_provider_credentials: false,
      records,
    }, null, 2)}\n`,
    "utf8",
  );
});

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`${locale} ${width}px Workbench and AI display contract`, async ({ browser }) => {
      browserVersion = browser.version();
      const context = await browser.newContext({ locale, viewport: { width, height: 900 } });
      const page = await context.newPage();
      await runCase(page, locale, width, "admin");
      await page.close();
      await context.close();
    });
  }
}

for (const locale of LOCALES) {
  test(`${locale} 1280px AI loading and error contract`, async ({ browser }) => {
    browserVersion = browser.version();
    const context = await browser.newContext({ locale, viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    await runCase(page, locale, 1280, "error");
    await page.close();
    await context.close();
  });

  test(`${locale} 1280px read-only Workbench contract`, async ({ browser }) => {
    browserVersion = browser.version();
    const context = await browser.newContext({ locale, viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    await runCase(page, locale, 1280, "read");
    await page.close();
    await context.close();
  });
}
