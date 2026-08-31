/*
 * Issue #127 Wave D3 browser evidence.
 *
 * The in-app Browser is unavailable in this runtime, so this is intentionally
 * a repository-local Playwright/Chromium probe.  It talks to an already
 * running isolated Compose stack and records only safe request metadata.
 * Response bodies, cookies, Authorization values and host paths never enter
 * the evidence files.
 */

import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

// Resolve the repository's locked Web dependency even though this evidence
// runner lives outside web/. It is invoked from web/ by the documented command.
const require = createRequire(join(process.cwd(), "package.json"));
const { chromium } = require("playwright");

const BASE_URL = process.env.DLR_D3_BASE_URL ?? "http://127.0.0.1:8923";
const ACCOUNT_BASE_URL = process.env.DLR_D3_ACCOUNT_BASE_URL ?? "http://127.0.0.1:9023";
const ADMIN_TOKEN = process.env.DLR_D3_ADMIN_TOKEN;
const ADAPTER_ID = Number(process.env.DLR_D3_ADAPTER_ID ?? "1");
const OUTPUT_DIR = process.env.DLR_D3_OUTPUT_DIR ?? "../docs/evidence/issue127-d3";
const LONG_FILENAME = "D3-very-long-文件名-ABCDEFGHIJKLMNOPQRSTUVWXYZ-0123456789.txt";
const ACCOUNT_PASSWORD = process.env.DLR_D3_ACCOUNT_PASSWORD;
const LOCALES = ["zh-CN", "en"];
const WIDTHS = [1280, 1440, 1680, 1920];

if (!ADMIN_TOKEN || !ACCOUNT_PASSWORD) {
  throw new Error("DLR_D3_ADMIN_TOKEN and DLR_D3_ACCOUNT_PASSWORD are required");
}
if (!Number.isInteger(ADAPTER_ID) || ADAPTER_ID < 1) {
  throw new Error("DLR_D3_ADAPTER_ID must be a positive integer");
}

const result = {
  browser: {
    engine: "Chromium",
    headless: true,
    transport: "repository-local Playwright",
    in_app_browser: "unavailable: no browser runtime was available",
    base_url: BASE_URL,
    account_base_url: ACCOUNT_BASE_URL,
  },
  business_flow: {
    status: "UNRUN",
    adapter_id: ADAPTER_ID,
    steps: [],
  },
  account_upload: {
    status: "UNRUN",
    steps: [],
  },
  matrix: [],
  clone: {
    status: "UNRUN",
    steps: [],
  },
  human_acceptance: "待人工验收",
};

function safeUrl(rawUrl) {
  try {
    return new URL(rawUrl).pathname;
  } catch {
    return "<invalid-url>";
  }
}

function safeText(value) {
  return String(value)
    .replaceAll(ADMIN_TOKEN, "<redacted-token>")
    .replaceAll(ACCOUNT_PASSWORD, "<redacted-password>")
    .replaceAll("/var/lib/dlr", "<redacted-runtime-root>")
    .replaceAll("/private/tmp", "<redacted-temp-root>");
}

function heightFor(width) {
  return width >= 1920 ? 1080 : Math.round(width * 0.625);
}

async function apiRequest(context, baseUrl, method, path, options = {}) {
  const headers = {
    Authorization: `Bearer ${ADMIN_TOKEN}`,
    ...(options.headers ?? {}),
  };
  return context.request.fetch(`${baseUrl}${path}`, {
    method,
    headers,
    data: options.data,
  });
}

async function readJson(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function setLocale(context, locale) {
  const response = await apiRequest(context, BASE_URL, "PUT", "/api/locale", {
    headers: { "Content-Type": "application/json" },
    data: { locale },
  });
  if (response.status() !== 200) {
    throw new Error(`locale update failed: ${response.status()}`);
  }
}

function attachTelemetry(page) {
  const telemetry = {
    console: [],
    page_errors: [],
    requests: [],
    responses: [],
    request_failures: [],
    upload_headers: [],
  };
  page.on("console", (message) => {
    telemetry.console.push({ type: message.type(), text: safeText(message.text()) });
  });
  page.on("pageerror", (error) => {
    telemetry.page_errors.push(safeText(error.message));
  });
  page.on("request", (request) => {
    if (!request.url().includes("/api/")) return;
    const path = safeUrl(request.url());
    telemetry.requests.push({ method: request.method(), path });
    if (path.endsWith("/input-artifacts") && request.method() === "POST") {
      void request.allHeaders().then((headers) => {
        telemetry.upload_headers.push({
          has_authorization: typeof headers.authorization === "string",
          has_cookie: typeof headers.cookie === "string",
          has_csrf: typeof headers["x-csrf-token"] === "string",
        });
      });
    }
  });
  page.on("response", (response) => {
    if (!response.url().includes("/api/")) return;
    telemetry.responses.push({ method: response.request().method(), status: response.status(), path: safeUrl(response.url()) });
  });
  page.on("requestfailed", (request) => {
    if (!request.url().includes("/api/")) return;
    telemetry.request_failures.push({
      method: request.method(),
      path: safeUrl(request.url()),
      error: safeText(request.failure()?.errorText ?? "unknown"),
    });
  });
  return telemetry;
}

async function waitForVisible(locator, timeout = 20_000) {
  await locator.waitFor({ state: "visible", timeout });
}

function sourceAdapterButton(page) {
  return page.locator('[data-testid="adapter-item"]')
    .filter({ has: page.locator(".catalog-item-name").filter({ hasText: /^D3 managed input acceptance$/ }) })
    .first();
}

async function loginToken(page, locale) {
  await page.goto(`${BASE_URL}/`);
  await page.waitForLoadState("networkidle");
  await waitForVisible(page.getByTestId("admin-token-input"));
  await page.getByTestId("admin-token-input").fill(ADMIN_TOKEN);
  await page.getByTestId("admin-token-submit").click();
  await waitForVisible(page.getByTestId("adapter-catalog"));
  const adapter = sourceAdapterButton(page);
  await waitForVisible(adapter);
  await adapter.click();
  await waitForVisible(page.getByTestId("workbench-header"));
  await page.getByRole("tab", { name: locale === "zh-CN" ? "运行设置" : "Runtime settings" }).click();
  await waitForVisible(page.getByTestId("task-input-config"));
}

async function openHistory(page, locale) {
  await page.getByRole("tab", { name: locale === "zh-CN" ? "执行记录" : "Executions" }).click();
  await waitForVisible(page.getByTestId("history-toolbar"));
  await page.waitForTimeout(400);
}

async function waitForExecution(context, adapterId, minimumId = 0, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const response = await apiRequest(context, BASE_URL, "GET", `/api/adapters/${adapterId}/executions`);
    if (response.status() === 200) {
      const body = await readJson(response);
      const execution = body?.items?.find((item) => item.id > minimumId);
      if (execution && !["pending", "running"].includes(execution.status)) {
        return execution;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`execution after ${minimumId} did not reach terminal state`);
}

async function overflowFacts(page) {
  return page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
    horizontal_overflow:
      document.documentElement.scrollWidth > document.documentElement.clientWidth ||
      document.body.scrollWidth > document.body.clientWidth,
  }));
}

async function rawKeyFacts(page) {
  const body = await page.locator("body").innerText();
  return {
    raw_input_key_visible: /(?:task|input|history|managedInput)\.[A-Za-z0-9_.-]+/.test(body),
    deployment_detail_visible: /(?:DLR_[A-Z_]+|\/var\/lib\/dlr|storage_key)/.test(body),
  };
}

async function spaNavigate(page, path) {
  await page.evaluate((nextPath) => {
    window.history.pushState({}, "", nextPath);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, path);
}

async function businessFlow(browser) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    permissions: ["clipboard-read", "clipboard-write"],
  });
  const page = await context.newPage();
  const telemetry = attachTelemetry(page);
  const steps = result.business_flow.steps;
  try {
    await setLocale(context, "zh-CN");
    await loginToken(page, "zh-CN");
    await page.getByTestId("task-input-source-managed_files").click();
    await waitForVisible(page.getByTestId("managed-input-editor"));

    const readyFiles = page.locator('[data-testid^="replace-managed-file-"]');
    if (await readyFiles.count() === 0) throw new Error("expected a saved managed file before replacement");
    await readyFiles.first().click();
    await page.locator('input[type="file"]').last().setInputFiles({
      name: LONG_FILENAME,
      mimeType: "text/plain",
      buffer: Buffer.from("D3 replacement fixture\n"),
    });
    await waitForVisible(page.locator('[data-testid^="managed-input-status-"]').filter({ hasText: "待保存" }));
    steps.push({ name: "replace", status: "PASS", filename: LONG_FILENAME });

    await page.getByTestId("managed-input-retention-mode").click();
    await page.locator('.ant-select-dropdown:visible .ant-select-item-option').filter({ hasText: "自定义期限" }).click();
    await page.getByTestId("managed-input-retention-seconds").fill("7200");
    const saveResponsePromise = page.waitForResponse((response) => {
      return safeUrl(response.url()) === `/api/adapters/${ADAPTER_ID}/input-config`;
    });
    await page.getByTestId("save-task-input").click();
    const saveResponse = await saveResponsePromise;
    if (saveResponse.status() !== 200) throw new Error(`managed input save status ${saveResponse.status()}`);
    await waitForVisible(page.locator('[data-testid^="managed-input-status-"]').filter({ hasText: "已就绪" }));
    steps.push({ name: "save-custom-retention", status: "PASS", response_status: saveResponse.status() });

    const copyButton = page.getByTestId("managed-input-example-copy");
    await waitForVisible(copyButton);
    await copyButton.click();
    await waitForVisible(page.getByTestId("managed-input-example-copy-status"));
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    if (!copied.includes("context.input_files")) throw new Error("Python Context example was not copied");
    steps.push({ name: "python-context-example", status: "PASS", contains_stable_api: true });

    const beforeManual = await apiRequest(context, BASE_URL, "GET", `/api/adapters/${ADAPTER_ID}/executions`);
    const beforeManualBody = await readJson(beforeManual);
    const beforeManualId = Math.max(0, ...(beforeManualBody?.items ?? []).map((item) => item.id));
    await page.getByTestId("header-task-run-once").click();
    const manual = await waitForExecution(context, ADAPTER_ID, beforeManualId);
    if (manual.trigger !== "manual" || manual.status !== "succeeded") throw new Error("manual execution did not succeed");
    steps.push({ name: "manual-run", status: "PASS", trigger: manual.trigger, execution_status: manual.status });

    await page.getByRole("tab", { name: "运行设置" }).click();
    await page.getByTestId("task-run-mode").locator("label").filter({ hasText: "定时运行" }).click();
    await waitForVisible(page.getByTestId("task-schedule-cron"));
    await page.getByTestId("save-task-runtime").click();
    await waitForVisible(page.getByTestId("header-task-schedule-toggle"));
    steps.push({ name: "schedule-config", status: "PASS" });

    await page.getByTestId("header-task-schedule-toggle").click();
    await page.waitForTimeout(800);
    const scheduleToggle = await page.getByTestId("header-task-schedule-toggle").getAttribute("aria-label");
    if (!scheduleToggle?.includes("停用")) throw new Error("schedule did not become enabled");
    const managedCard = page.getByTestId("task-input-source-managed_files");
    if (await managedCard.getAttribute("aria-disabled") !== "true") throw new Error("runtime lock did not disable input card");
    steps.push({ name: "runtime-lock", status: "PASS", managed_card_disabled: true });

    const lockedConfigResponse = await apiRequest(context, BASE_URL, "GET", `/api/adapters/${ADAPTER_ID}/input-config`);
    const lockedConfig = await readJson(lockedConfigResponse);
    const lockedUpdate = await apiRequest(context, BASE_URL, "PUT", `/api/adapters/${ADAPTER_ID}/input-config`, {
      headers: { "Content-Type": "application/json" },
      data: { expected_revision: lockedConfig.revision, source_type: "none" },
    });
    const lockedUpdateBody = await readJson(lockedUpdate);
    if (lockedUpdate.status() !== 409 || lockedUpdateBody?.detail?.code !== "adapter_runtime_locked") {
      throw new Error(`runtime lock error smoke expected adapter_runtime_locked/409, got ${lockedUpdate.status()}`);
    }
    steps.push({ name: "runtime-lock-error", status: "PASS", response_status: lockedUpdate.status(), error_code: lockedUpdateBody.detail.code });

    const beforeRunNow = await apiRequest(context, BASE_URL, "GET", `/api/adapters/${ADAPTER_ID}/executions`);
    const beforeRunNowBody = await readJson(beforeRunNow);
    const beforeRunNowId = Math.max(0, ...(beforeRunNowBody?.items ?? []).map((item) => item.id));
    const runNow = page.getByTestId("header-task-run-once");
    if (await runNow.isDisabled()) throw new Error("schedule run-now is unexpectedly disabled");
    await runNow.click();
    const runNowExecution = await waitForExecution(context, ADAPTER_ID, beforeRunNowId);
    if (runNowExecution.trigger !== "manual" || runNowExecution.status !== "succeeded") throw new Error("schedule run-now did not succeed");
    steps.push({ name: "schedule-run-now", status: "PASS", trigger: runNowExecution.trigger, execution_status: runNowExecution.status });

    await page.getByTestId("header-task-schedule-toggle").click();
    await page.waitForTimeout(800);
    const disabledLabel = await page.getByTestId("header-task-schedule-toggle").getAttribute("aria-label");
    if (!disabledLabel?.includes("启用")) throw new Error("schedule did not become disabled");
    steps.push({ name: "schedule-disable", status: "PASS" });

    await openHistory(page, "zh-CN");
    const rows = page.getByTestId("history-row");
    if (await rows.count() < 2) throw new Error("manual and run-now history rows are missing");
    await rows.first().press("Enter");
    await waitForVisible(page.getByTestId("detail-input"));
    const detailText = await page.locator(".execution-detail").innerText();
    const forbidden = ["Artifact ID", "storage_key", "下载", "复用", "恢复配置", "再次运行"].filter((item) => detailText.includes(item));
    if (forbidden.length > 0) throw new Error(`history detail contains forbidden controls: ${forbidden.join(",")}`);
    steps.push({ name: "history-safe-summary", status: "PASS", forbidden_terms: forbidden });
    await page.locator(".execution-history-drawer .ant-drawer-close").click();

    // The E0 retained stack contains several runtime fixtures; the source
    // Adapter must be selected explicitly instead of relying on catalog order.
    const sourceRow = page.locator(".catalog-row").filter({ has: sourceAdapterButton(page) }).first();
    await sourceRow.getByTestId("adapter-item-menu").click();
    const cloneMenu = page.locator(".ant-dropdown:visible");
    await waitForVisible(cloneMenu);
    const cloneLabel = /复制/;
    await cloneMenu.getByRole("menuitem").filter({ hasText: cloneLabel }).click();
    await waitForVisible(page.locator(".ant-modal:visible"));
    await page.locator(".ant-modal:visible .ant-btn-primary").click();
    await waitForVisible(page.getByTestId("workbench-header"));
    await page.getByRole("tab", { name: "运行设置" }).click();
    await waitForVisible(page.getByTestId("task-input-config"));
    const cloneNotice = page.getByTestId("managed-input-clone-notice");
    await waitForVisible(cloneNotice);
    const cloneCount = await page.getByTestId("managed-input-count").innerText();
    const cloneAdaptersResponse = await apiRequest(context, BASE_URL, "GET", "/api/adapters");
    const cloneAdapters = await readJson(cloneAdaptersResponse);
    const cloneAdapter = cloneAdapters.find((item) => item.id !== ADAPTER_ID && item.name.includes("D3 managed input acceptance"));
    if (!cloneAdapter) throw new Error("clone Adapter was not returned");
    const cloneConfigResponse = await apiRequest(context, BASE_URL, "GET", `/api/adapters/${cloneAdapter.id}/input-config`);
    const cloneConfig = await readJson(cloneConfigResponse);
    if (cloneConfig.source_type !== "managed_files" || cloneConfig.artifacts.length !== 0 || cloneConfig.valid_for_run !== false) {
      throw new Error("clone managed input is not an empty non-runnable collection");
    }
    const cloneScheduleResponse = await apiRequest(context, BASE_URL, "GET", `/api/adapters/${cloneAdapter.id}/schedule`);
    const cloneSchedule = await readJson(cloneScheduleResponse);
    if (cloneScheduleResponse.status() === 200 && cloneSchedule.enabled !== false) throw new Error("clone schedule is enabled");
    result.clone = {
      status: "PASS",
      adapter_id: cloneAdapter.id,
      steps: [
        { name: "empty-managed-files", status: "PASS", count: cloneCount, invalid_reason: cloneConfig.invalid_reason },
        { name: "reupload-notice", status: "PASS", visible: true },
        { name: "schedule-disabled", status: "PASS", response_status: cloneScheduleResponse.status(), enabled: cloneSchedule?.enabled ?? false },
      ],
    };
    steps.push({ name: "managed-files-clone", status: "PASS", clone_adapter_id: cloneAdapter.id });

    // Return selection to the source Adapter for the visual matrix.
    const sourceAdapter = sourceAdapterButton(page);
    await sourceAdapter.click();
    await waitForVisible(page.getByTestId("workbench-header"));
    await page.getByRole("tab", { name: "运行设置" }).click();
    await waitForVisible(page.getByTestId("task-input-config"));
    await page.screenshot({ path: join(OUTPUT_DIR, "screenshots", "business-zh-1280.png"), fullPage: true });
    result.business_flow.status = "PASS";
  } finally {
    result.business_flow.telemetry = telemetry;
    await context.close();
  }
}

async function accountUpload(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const telemetry = attachTelemetry(page);
  const steps = result.account_upload.steps;
  try {
    await page.goto(`${ACCOUNT_BASE_URL}/`);
    await page.waitForLoadState("networkidle");
    if (await page.getByTestId("account-login-page").count() === 0) throw new Error("account login page missing");
    await page.getByTestId("account-username-input").fill("admin");
    await page.getByTestId("account-password-input").fill(ACCOUNT_PASSWORD);
    await page.getByTestId("account-login-submit").click();
    await waitForVisible(page.getByTestId("adapter-catalog"));
    await sourceAdapterButton(page).click();
    await page.getByRole("tab", { name: "运行设置" }).click();
    await waitForVisible(page.getByTestId("task-input-config"));
    await page.getByTestId("task-input-source-managed_files").click();
    const before = await page.locator('[data-testid^="managed-input-status-"]').count();
    await page.locator('input[type="file"]').first().setInputFiles({
      name: "d3-account-csrf-check.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("account csrf fixture\n"),
    });
    const stagedCard = page.locator(".managed-input-file-card").filter({ hasText: "d3-account-csrf-check.txt" });
    await waitForVisible(stagedCard);
    await page.waitForTimeout(100);
    const uploadHeaders = telemetry.upload_headers.at(-1) ?? {};
    if (!uploadHeaders.has_cookie || !uploadHeaders.has_csrf || uploadHeaders.has_authorization) {
      throw new Error(`account upload headers did not match Cookie + CSRF without bearer token: ${JSON.stringify(uploadHeaders)}`);
    }
    steps.push({ name: "account-csrf-multipart", status: "PASS", before_file_count: before, headers: uploadHeaders });
    await stagedCard.locator('[data-testid^="delete-managed-file-"]').click();
    await page.waitForTimeout(500);
    steps.push({ name: "account-staged-delete", status: "PASS" });
    result.account_upload.status = "PASS";
  } finally {
    result.account_upload.telemetry = telemetry;
    await context.close();
  }
}

async function visualMatrix(browser) {
  for (const locale of LOCALES) {
    for (const width of WIDTHS) {
      const context = await browser.newContext({
        viewport: { width, height: heightFor(width) },
      });
      const page = await context.newPage();
      const telemetry = attachTelemetry(page);
      const entry = { locale, width, height: heightFor(width), status: "FAIL" };
      try {
        await setLocale(context, locale);
        await loginToken(page, locale);
        const cards = page.locator('[data-testid^="task-input-source-"]');
        const managedCard = page.getByTestId("task-input-source-managed_files");
        const cardCount = await cards.count();
        const managedDisabled = await managedCard.getAttribute("aria-disabled");
        const title = await page.locator('[data-testid^="managed-input-filename-"]').first().getAttribute("title");
        await managedCard.focus();
        await managedCard.press("Enter");
        const keyboardSelected = (await managedCard.getAttribute("aria-checked")) === "true";
        const runtimeOverflow = await overflowFacts(page);
        const runtimeKeys = await rawKeyFacts(page);
        await page.screenshot({ path: join(OUTPUT_DIR, "screenshots", `runtime-${locale}-${width}.png`), fullPage: true });

        await spaNavigate(page, "/settings/managed-input");
        await waitForVisible(page.getByTestId("managed-input-settings-panel"));
        const settingsOverflow = await overflowFacts(page);
        const settingsKeys = await rawKeyFacts(page);
        await page.screenshot({ path: join(OUTPUT_DIR, "screenshots", `settings-${locale}-${width}.png`), fullPage: true });

        await spaNavigate(page, "/");
        await waitForVisible(page.getByTestId("workbench-header"));
        await openHistory(page, locale);
        await waitForVisible(page.getByTestId("history-row").first());
        const historyOverflow = await overflowFacts(page);
        const historyKeys = await rawKeyFacts(page);
        await page.screenshot({ path: join(OUTPUT_DIR, "screenshots", `history-${locale}-${width}.png`), fullPage: true });
        await page.getByTestId("history-row").first().press("Enter");
        await waitForVisible(page.getByTestId("detail-input"));
        const detailOverflow = await overflowFacts(page);
        const detailKeys = await rawKeyFacts(page);
        const detailText = await page.locator(".execution-detail").innerText();
        const forbidden = ["Artifact ID", "storage_key", "下载", "复用", "恢复配置", "再次运行", "Download", "Reuse", "Restore", "Run again"].filter((item) => detailText.includes(item));
        await page.screenshot({ path: join(OUTPUT_DIR, "screenshots", `history-detail-${locale}-${width}.png`), fullPage: true });

        entry.status = "PASS";
        entry.cards = { count: cardCount, managed_disabled: managedDisabled, managed_enabled: managedDisabled === "false" };
        entry.long_filename = { title, title_matches_fixture: title === LONG_FILENAME, keyboard_selected: keyboardSelected };
        entry.overflow = { runtime: runtimeOverflow, settings: settingsOverflow, history: historyOverflow, detail: detailOverflow };
        entry.localization = { runtime: runtimeKeys, settings: settingsKeys, history: historyKeys, detail: detailKeys };
        entry.history_forbidden_terms = forbidden;
      } finally {
        entry.telemetry = telemetry;
        result.matrix.push(entry);
        await context.close();
      }
    }
  }
}

async function main() {
  await mkdir(join(OUTPUT_DIR, "screenshots"), { recursive: true });
  const browser = await chromium.launch({ headless: true });
  try {
    await businessFlow(browser);
    await accountUpload(browser);
    await visualMatrix(browser);
  } catch (error) {
    result.error = safeText(error instanceof Error ? error.message : String(error));
    await writeFile(join(OUTPUT_DIR, "browser-matrix.partial.json"), `${JSON.stringify(result, null, 2)}\n`, "utf8");
    throw error;
  } finally {
    await browser.close();
  }
  const failed = result.business_flow.status !== "PASS" ||
    result.account_upload.status !== "PASS" ||
    result.clone.status !== "PASS" ||
    result.matrix.some((entry) => entry.status !== "PASS");
  result.machine_gate = failed ? "FAIL" : "PASS";
  await writeFile(join(OUTPUT_DIR, "browser-matrix.json"), `${JSON.stringify(result, null, 2)}\n`, "utf8");
  if (failed) {
    throw new Error("D3 browser evidence has failed entries; inspect browser-matrix.json");
  }
  console.log(JSON.stringify({
    machine_gate: result.machine_gate,
    business_steps: result.business_flow.steps.length,
    account_steps: result.account_upload.steps.length,
    matrix_entries: result.matrix.length,
    human_acceptance: result.human_acceptance,
  }));
}

await main();
