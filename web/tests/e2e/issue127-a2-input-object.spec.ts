import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page, type Request, type Route } from "@playwright/test";

type Locale = "zh-CN" | "en";
type InputSource = "none" | "json";

const LOCALES: readonly Locale[] = ["zh-CN", "en"];
const VIEWPORTS = [1280, 1920] as const;
const specDir = dirname(fileURLToPath(import.meta.url));
const evidenceRoot = resolve(specDir, "../../test-results/issue127-a2");
const screenshotRoot = resolve(evidenceRoot, "screenshots");

const worker = {
  id: 1,
  name: "A2 fixture worker",
  status: "online",
  last_heartbeat: "2026-08-26T00:00:00Z",
  capabilities: ["python", "javascript", "java"],
};

const adapterBase = {
  id: 1,
  name: "A2 anonymous task",
  description: "Anonymous Input Object fixture",
  language: "python",
  adapter_type: "task",
  run_mode: "schedule",
  timeout_seconds: 300,
  owner_user_id: 42,
  owner_username: "fixture-owner",
  latest_version_id: 10,
  runtime_worker_id: 1,
  runtime_locked: false,
  archived_at: null,
  running_execution_id: null as number | null,
  created_at: "2026-08-26T00:00:00Z",
  updated_at: "2026-08-26T00:00:00Z",
  access_level: "admin",
};

const versionSummary = {
  id: 10,
  adapter_id: 1,
  seq: 1,
  created_at: "2026-08-26T00:00:00Z",
};

const version = {
  ...versionSummary,
  code: "def handle(context, input):\n    return {\"received\": input}\n",
  requirements: "",
  runtime_config: {},
};

const jsonValues: ReadonlyArray<{ label: string; value: unknown }> = [
  { label: "object", value: { region: "cn-east", enabled: true } },
  { label: "array", value: ["cn-east", 7] },
  { label: "string", value: "fixture-string" },
  { label: "number", value: 42 },
  { label: "boolean", value: false },
  { label: "null", value: null },
];

interface InputState {
  revision: number;
  sourceType: InputSource;
  jsonValue: unknown;
}

interface RequestRecord {
  method: string;
  path: string;
  status: number;
  body_shape?: "input_config_payload" | "contains_input_field" | "empty_json_object" | "non_empty_json_object";
}

interface BrowserRecord {
  locale: Locale;
  width: number;
  height: number;
  screenshot: string;
  json_top_level_values: string[];
  revisions_seen: number[];
  draft_preserved_after_mode_switch: boolean;
  revision_conflict: {
    status: 409;
    draft_preserved: boolean;
    revision_preserved: boolean;
  };
  run_now: {
    status: number;
    body_shape: "empty_json_object";
    input_field_present: false;
  };
  source_cards: {
    count: number;
    all_focusable: boolean;
    managed_files_disabled: boolean;
    remote_files_disabled: boolean;
  };
  diagnostics: {
    console_errors: number;
    expected_console_errors: number;
    page_errors: number;
    unexpected_http_errors: number;
    unknown_requests: number;
    file_requests: number;
  };
  overflow: {
    inner_width: number;
    document_scroll_width: number;
    body_scroll_width: number;
  };
  request_metadata: RequestRecord[];
  raw_translation_key_visible: false;
}

const records: BrowserRecord[] = [];
let browserVersion = "unknown";

function jsonBody(body: unknown): string {
  return JSON.stringify(body);
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: jsonBody(body),
  });
}

function errorBody(code: string, message: string, params: Record<string, unknown> = {}) {
  return { detail: { code, message, params } };
}

function inputConfig(state: InputState) {
  return {
    adapter_id: 1,
    revision: state.revision,
    source_type: state.sourceType,
    json_value: state.sourceType === "json" ? state.jsonValue : null,
    retention: { mode: "system_default", seconds: null },
    artifacts: [],
    valid_for_run: true,
    invalid_reason: null,
  };
}

function adapter(state: { executionActive: boolean }) {
  return {
    ...adapterBase,
    runtime_locked: state.executionActive,
    running_execution_id: state.executionActive ? 31 : null,
  };
}

function execution(state: InputState, status: "pending" | "succeeded" = "pending") {
  return {
    id: 31,
    adapter_id: 1,
    version_id: 10,
    worker_id: 1,
    target_worker_id: 1,
    trigger: "manual",
    scheduled_for: null,
    status,
    input: state.sourceType === "json" ? state.jsonValue : null,
    output: status === "succeeded" ? { ok: true } : null,
    output_size: status === "succeeded" ? 11 : null,
    output_truncated: false,
    output_preview: null,
    stdout: status === "succeeded" ? "A2 fixture run complete\\n" : "",
    stdout_truncated: false,
    stderr: "",
    stderr_truncated: false,
    error: null,
    locale: "zh-CN",
    created_at: "2026-08-26T00:00:00Z",
    started_at: status === "pending" ? null : "2026-08-26T00:00:01Z",
    ended_at: status === "succeeded" ? "2026-08-26T00:00:02Z" : null,
    duration_ms: status === "succeeded" ? 1000 : null,
  };
}

function requestBodyShape(path: string, method: string, request: Request): RequestRecord["body_shape"] {
  if (method !== "PUT" && method !== "POST") {
    return undefined;
  }
  let body: unknown;
  try {
    body = request.postDataJSON();
  } catch {
    return undefined;
  }
  if (path === "/api/adapters/1/executions" && method === "POST") {
    return typeof body === "object" && body !== null && !Array.isArray(body) && Object.keys(body).length === 0
      ? "empty_json_object"
      : "non_empty_json_object";
  }
  if (path === "/api/adapters/1/input-config" && method === "PUT") {
    return typeof body === "object" && body !== null && Object.prototype.hasOwnProperty.call(body, "input")
      ? "contains_input_field"
      : "input_config_payload";
  }
  return undefined;
}

async function installFixture(page: Page, locale: Locale) {
  const state: InputState = { revision: 1, sourceType: "none", jsonValue: null };
  const runtime = { executionActive: false };
  const fixture = { conflictNext: false };
  const unknownRequests: string[] = [];
  const requestMetadata: RequestRecord[] = [];

  page.on("response", (response) => {
    const url = new URL(response.url());
    if (!url.pathname.startsWith("/api/")) {
      return;
    }
    const request = response.request();
    requestMetadata.push({
      method: request.method(),
      path: url.pathname,
      status: response.status(),
      body_shape: requestBodyShape(url.pathname, request.method(), request),
    });
  });

  await page.route("**/entry-mode.js", async (route) => {
    await route.fulfill({
      contentType: "application/javascript",
      body: 'window.__DLR_ENTRY_MODE__ = "token";',
    });
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const path = new URL(request.url()).pathname;

    if (path === "/api/locale" && method === "GET") return fulfillJson(route, { locale });
    if (path === "/api/health" && method === "GET") return fulfillJson(route, { status: "ok", database: true });
    if (path === "/api/auth/admin/verify" && method === "GET") return fulfillJson(route, { status: "ok" });
    if (path === "/api/workers" && method === "GET") return fulfillJson(route, [worker]);
    if (path === "/api/adapters" && method === "GET") return fulfillJson(route, [adapter(runtime)]);
    if (path === "/api/adapters/1" && method === "GET") return fulfillJson(route, adapter(runtime));
    if (path === "/api/adapters/1/versions" && method === "GET") return fulfillJson(route, [versionSummary]);
    if (path === "/api/adapters/1/versions/10" && method === "GET") return fulfillJson(route, version);
    if (path === "/api/adapters/1/credential-bindings" && method === "GET") return fulfillJson(route, []);
    if (path === "/api/adapters/1/credential-options" && method === "GET") return fulfillJson(route, []);
    if (path === "/api/adapters/1/schedule" && method === "GET") {
      return fulfillJson(route, {
        adapter_id: 1,
        enabled: false,
        cron: "*/5 * * * *",
        timezone: "Asia/Shanghai",
        input: null,
        next_run_at: null,
        updated_at: "2026-08-26T00:00:00Z",
      });
    }
    if (path === "/api/adapters/1/input-config" && method === "GET") return fulfillJson(route, inputConfig(state));
    if (path === "/api/adapters/1/input-config" && method === "PUT") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      if (fixture.conflictNext) {
        fixture.conflictNext = false;
        return fulfillJson(route, errorBody(
          "input_config_revision_conflict",
          "fixture conflict",
          { expected_revision: payload.expected_revision, current_revision: state.revision + 1 },
        ), 409);
      }
      if (payload.source_type === "none" && !Object.prototype.hasOwnProperty.call(payload, "json_value")) {
        state.sourceType = "none";
        state.jsonValue = null;
      } else if (payload.source_type === "json" && Object.prototype.hasOwnProperty.call(payload, "json_value")) {
        state.sourceType = "json";
        state.jsonValue = payload.json_value;
      } else if (payload.source_type === "remote_files") {
        return fulfillJson(route, errorBody("input_source_not_available", "fixture remote disabled"), 422);
      } else {
        return fulfillJson(route, errorBody("input_invalid", "fixture invalid input"), 422);
      }
      state.revision += 1;
      return fulfillJson(route, inputConfig(state));
    }
    if (path === "/api/adapters/1/executions" && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      if (Object.keys(payload).length !== 0) {
        return fulfillJson(route, errorBody("execution_input_override_not_supported", "fixture input override"), 422);
      }
      runtime.executionActive = true;
      return fulfillJson(route, execution(state), 202);
    }
    if (path === "/api/executions/31/events" && method === "GET") {
      runtime.executionActive = false;
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "cache-control": "no-cache" },
        body: `event: execution\ndata: ${jsonBody(execution(state, "succeeded"))}\n\n`,
      });
    }
    if (path === "/api/executions/31" && method === "GET") return fulfillJson(route, execution(state, "succeeded"));
    if (path === "/api/adapters/1/executions" && method === "GET") return fulfillJson(route, { items: [], next_before_id: null });

    unknownRequests.push(`${method} ${path}`);
    return fulfillJson(route, errorBody("fixture_unhandled_request", "fixture route not defined"), 404);
  });

  return { fixture, unknownRequests, requestMetadata };
}

async function loginAndOpenRuntime(page: Page, locale: Locale) {
  await page.goto("/");
  await page.waitForLoadState("networkidle");
  await expect(page.getByTestId("admin-token-input")).toBeVisible();
  await page.getByTestId("admin-token-input").fill("fixture-token");
  await page.getByTestId("admin-token-submit").click();
  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
  await page.getByTestId("adapter-item").first().click();
  await expect(page.getByTestId("workbench-header")).toBeVisible();
  await page.getByRole("tab", { name: locale === "zh-CN" ? "运行设置" : "Runtime settings" }).click();
  await expect(page.getByTestId("task-input-config")).toBeVisible();
  await expect(page.getByTestId("task-input-revision")).toContainText("1");
}

async function measureOverflow(page: Page) {
  return page.evaluate(() => ({
    inner_width: window.innerWidth,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
  }));
}

test.beforeAll(async ({ browser }) => {
  browserVersion = browser.version();
});

test.afterAll(() => {
  mkdirSync(screenshotRoot, { recursive: true });
  writeFileSync(
    resolve(evidenceRoot, "browser-matrix.json"),
    `${JSON.stringify({
      schema: "issue127-a2-browser-matrix-v1",
      runner: "@playwright/test 1.62.1",
      browser: "chromium",
      browser_version: browserVersion,
      cases: records,
    }, null, 2)}\n`,
  );
});

test.describe("Issue #127 A2 unified Input Object", () => {
  test.describe.configure({ mode: "serial" });

  for (const locale of LOCALES) {
    for (const width of VIEWPORTS) {
      test(`${locale} ${width}px`, async ({ page }) => {
        const height = width === 1280 ? 720 : 1080;
        await page.setViewportSize({ width, height });
        const consoleErrors: string[] = [];
        const expectedConsoleErrors: string[] = [];
        const pageErrors: string[] = [];
        page.on("console", (message) => {
          if (message.type() !== "error") {
            return;
          }
          if (message.text().includes("status of 409")) {
            expectedConsoleErrors.push("expected input revision conflict");
          } else {
            consoleErrors.push(message.text());
          }
        });
        page.on("pageerror", (error) => pageErrors.push(error.message));
        const { fixture, unknownRequests, requestMetadata } = await installFixture(page, locale);

        await loginAndOpenRuntime(page, locale);
        await expect(page.locator("html")).toHaveAttribute("lang", locale);

        const sourceCards = [
          page.getByTestId("task-input-source-none"),
          page.getByTestId("task-input-source-json"),
          page.getByTestId("task-input-source-managed_files"),
          page.getByTestId("task-input-source-remote_files"),
        ];
        for (const card of sourceCards) {
          await expect(card).toHaveAttribute("tabindex", "0");
          await card.focus();
          await expect(card).toBeFocused();
        }
        await expect(page.getByTestId("task-input-source-managed_files")).toHaveAttribute("aria-disabled", "true");
        await expect(page.getByTestId("task-input-source-remote_files")).toHaveAttribute("aria-disabled", "true");
        await page.getByTestId("task-input-source-json").click();
        const jsonInput = page.getByTestId("task-input-json");
        await expect(jsonInput).toBeVisible();
        await expect(jsonInput).toHaveAttribute(
          "placeholder",
          locale === "zh-CN" ? "例如 {\"region\":\"cn-east\"}" : "e.g. {\"region\":\"cn-east\"}",
        );

        const managedCard = page.getByTestId("task-input-source-managed_files");
        const remoteCard = page.getByTestId("task-input-source-remote_files");
        await managedCard.focus();
        await page.keyboard.press("Enter");
        await remoteCard.focus();
        await page.keyboard.press(" ");
        await expect(page.getByTestId("task-input-source-json")).toHaveAttribute("aria-checked", "true");

        let revision = 1;
        const revisionsSeen = [revision];
        await page.getByTestId("task-input-source-none").click();
        await expect(page.getByTestId("task-input-json")).toHaveCount(0);
        const noneSaveResponse = page.waitForResponse((response) => {
          return response.request().method() === "PUT" && new URL(response.url()).pathname === "/api/adapters/1/input-config";
        });
        await page.getByTestId("save-task-input").click();
        const noneSave = await noneSaveResponse;
        expect(noneSave.status()).toBe(200);
        revision += 1;
        revisionsSeen.push(revision);
        await expect(page.getByTestId("task-input-revision")).toContainText(String(revision));
        await page.getByTestId("task-input-source-json").click();
        await expect(jsonInput).toBeVisible();

        for (const item of jsonValues) {
          await jsonInput.fill(JSON.stringify(item.value));
          await expect(page.getByTestId("task-input-state")).toContainText(locale === "zh-CN" ? "草稿" : "Draft");
          const saveResponse = page.waitForResponse((response) => {
            return response.request().method() === "PUT" && new URL(response.url()).pathname === "/api/adapters/1/input-config";
          });
          await page.getByTestId("save-task-input").click();
          const response = await saveResponse;
          expect(response.status()).toBe(200);
          revision += 1;
          revisionsSeen.push(revision);
          await expect(page.getByTestId("task-input-revision")).toContainText(String(revision));
        }

        await jsonInput.fill('{"dirty":true}');
        await expect(page.getByTestId("task-input-state")).toContainText(locale === "zh-CN" ? "草稿" : "Draft");
        const runNow = page.getByTestId("header-task-run-once");
        await expect(runNow).toBeDisabled();

        await page.getByRole("radio", { name: locale === "zh-CN" ? "手动运行" : "Manual" }).click();
        await expect(page.getByTestId("task-schedule-cron")).toHaveCount(0);
        await page.getByRole("radio", { name: locale === "zh-CN" ? "定时运行" : "Scheduled" }).click();
        await expect(page.getByTestId("task-schedule-cron")).toBeVisible();
        await expect(jsonInput).toHaveValue('{"dirty":true}');

        fixture.conflictNext = true;
        const conflictResponse = page.waitForResponse((response) => {
          return response.request().method() === "PUT" && new URL(response.url()).pathname === "/api/adapters/1/input-config";
        });
        await page.getByTestId("save-task-input").click();
        const conflict = await conflictResponse;
        expect(conflict.status()).toBe(409);
        await expect(page.getByTestId("task-input-state")).toContainText(locale === "zh-CN" ? "草稿" : "Draft");
        await expect(jsonInput).toHaveValue('{"dirty":true}');
        await expect(page.getByTestId("task-input-revision")).toContainText(String(revision));
        await expect(page.getByTestId("error-banner")).toContainText(
          locale === "zh-CN" ? "输入对象已被其他页面更新" : "Input Object was updated by another page",
        );

        const finalSaveResponse = page.waitForResponse((response) => {
          return response.request().method() === "PUT" && new URL(response.url()).pathname === "/api/adapters/1/input-config";
        });
        await page.getByTestId("save-task-input").click();
        const finalSave = await finalSaveResponse;
        expect(finalSave.status()).toBe(200);
        revision += 1;
        revisionsSeen.push(revision);
        await expect(page.getByTestId("task-input-state")).toContainText(locale === "zh-CN" ? "已保存" : "Saved");
        await expect(page.getByTestId("task-input-revision")).toContainText(String(revision));

        const screenshotName = `input-object-${locale}-${width}.png`;
        mkdirSync(screenshotRoot, { recursive: true });
        await page.screenshot({
          path: resolve(screenshotRoot, screenshotName),
          fullPage: true,
          animations: "disabled",
        });

        await expect(runNow).toBeEnabled();
        const runResponse = page.waitForResponse((response) => {
          return response.request().method() === "POST" && new URL(response.url()).pathname === "/api/adapters/1/executions";
        });
        await runNow.click();
        const run = await runResponse;
        expect(run.status()).toBe(202);
        expect(run.request().postDataJSON()).toEqual({});
        expect(String(run.request().postData())).not.toContain("input");
        await expect(page.getByTestId("live-log")).toBeVisible();

        const overflow = await measureOverflow(page);
        expect(overflow.document_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
        expect(overflow.body_scroll_width).toBeLessThanOrEqual(overflow.inner_width);

        const fileRequests = requestMetadata.filter((request) => /\/(?:files?|upload)(?:\/|$)/.test(request.path));
        const unexpectedHttpErrors = requestMetadata.filter((request) => request.status >= 400 && !(request.path === "/api/adapters/1/input-config" && request.status === 409));
        expect(fileRequests).toEqual([]);
        expect(unknownRequests).toEqual([]);
        expect(unexpectedHttpErrors).toEqual([]);
        expect(consoleErrors).toEqual([]);
        expect(expectedConsoleErrors.length).toBeGreaterThanOrEqual(1);
        expect(pageErrors).toEqual([]);
        const visibleText = await page.locator("body").innerText();
        expect(visibleText).not.toContain("task.input.");
        expect(visibleText).not.toContain("input.sources.");

        const inputWrites = requestMetadata.filter((request) => request.path === "/api/adapters/1/input-config" && request.method === "PUT");
        expect(inputWrites.every((request) => request.body_shape === "input_config_payload")).toBe(true);
        const runMetadata = requestMetadata.find((request) => request.path === "/api/adapters/1/executions" && request.method === "POST");
        expect(runMetadata?.body_shape).toBe("empty_json_object");

        records.push({
          locale,
          width,
          height,
          screenshot: `screenshots/${screenshotName}`,
          json_top_level_values: jsonValues.map((item) => item.label),
          revisions_seen: revisionsSeen,
          draft_preserved_after_mode_switch: true,
          revision_conflict: {
            status: 409,
            draft_preserved: true,
            revision_preserved: true,
          },
          run_now: {
            status: run.status(),
            body_shape: "empty_json_object",
            input_field_present: false,
          },
          source_cards: {
            count: sourceCards.length,
            all_focusable: true,
            managed_files_disabled: true,
            remote_files_disabled: true,
          },
          diagnostics: {
            console_errors: consoleErrors.length,
            expected_console_errors: expectedConsoleErrors.length,
            page_errors: pageErrors.length,
            unexpected_http_errors: unexpectedHttpErrors.length,
            unknown_requests: unknownRequests.length,
            file_requests: fileRequests.length,
          },
          overflow,
          request_metadata: requestMetadata,
          raw_translation_key_visible: false,
        });
      });
    }
  }
});
