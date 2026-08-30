/*
 * Verify the retained Issue #127 app is reachable through both browser entry
 * points. This is a health handoff only: it never writes a user acceptance
 * result and it never stores Authorization/password values in evidence.
 */

import { createRequire } from "node:module";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

const require = createRequire(join(process.cwd(), "package.json"));
const { chromium } = require("playwright");

const BASE_URL = process.env.DLR_E1_BASE_URL ?? "http://127.0.0.1:8924";
const ACCOUNT_BASE_URL = process.env.DLR_E1_ACCOUNT_BASE_URL ?? "http://127.0.0.1:9024";
const ADMIN_TOKEN = process.env.DLR_E1_ADMIN_TOKEN;
const ACCOUNT_PASSWORD = process.env.DLR_E1_ACCOUNT_PASSWORD;
const OUTPUT = process.env.DLR_E1_OUTPUT ?? "../docs/evidence/issue127-e1/app-ready.json";

if (!ADMIN_TOKEN || !ACCOUNT_PASSWORD) {
  throw new Error("DLR_E1_ADMIN_TOKEN and DLR_E1_ACCOUNT_PASSWORD are required");
}

function safeText(value) {
  return String(value)
    .replaceAll(ADMIN_TOKEN, "<redacted-token>")
    .replaceAll(ACCOUNT_PASSWORD, "<redacted-password>");
}

function safePath(rawUrl) {
  try {
    return new URL(rawUrl).pathname;
  } catch {
    return "<invalid-url>";
  }
}

function telemetry(page) {
  const facts = { console_errors: [], page_errors: [], request_failures: [] };
  page.on("console", (message) => {
    if (message.type() === "error") facts.console_errors.push(safeText(message.text()));
  });
  page.on("pageerror", (error) => facts.page_errors.push(safeText(error.message)));
  page.on("requestfailed", (request) => facts.request_failures.push({
    method: request.method(),
    path: safePath(request.url()),
    error: safeText(request.failure()?.errorText ?? "unknown"),
  }));
  return facts;
}

async function waitVisible(locator) {
  await locator.waitFor({ state: "visible", timeout: 20_000 });
}

async function checkWeb(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const facts = telemetry(page);
  try {
    const response = await page.goto(`${BASE_URL}/`);
    await page.waitForLoadState("networkidle");
    await waitVisible(page.getByTestId("admin-token-input"));
    await page.getByTestId("admin-token-input").fill(ADMIN_TOKEN);
    await page.getByTestId("admin-token-submit").click();
    await waitVisible(page.getByTestId("adapter-catalog"));
    await waitVisible(page.locator('[data-testid="adapter-item"]').first());
    const adapterCount = await page.locator('[data-testid="adapter-item"]').count();
    return {
      status: "PASS",
      url: safePath(page.url()),
      initial_http_status: response?.status() ?? null,
      adapter_catalog_visible: true,
      adapter_count: adapterCount,
      telemetry: facts,
    };
  } finally {
    await context.close();
  }
}

async function checkAccount(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const facts = telemetry(page);
  try {
    const response = await page.goto(`${ACCOUNT_BASE_URL}/`);
    await page.waitForLoadState("networkidle");
    await waitVisible(page.getByTestId("account-login-page"));
    await page.getByTestId("account-username-input").fill("admin");
    await page.getByTestId("account-password-input").fill(ACCOUNT_PASSWORD);
    await page.getByTestId("account-login-submit").click();
    await waitVisible(page.getByTestId("adapter-catalog"));
    await waitVisible(page.locator('[data-testid="adapter-item"]').first());
    const adapterCount = await page.locator('[data-testid="adapter-item"]').count();
    return {
      status: "PASS",
      url: safePath(page.url()),
      initial_http_status: response?.status() ?? null,
      account_catalog_visible: true,
      adapter_count: adapterCount,
      telemetry: facts,
    };
  } finally {
    await context.close();
  }
}

await mkdir(join(process.cwd(), OUTPUT, ".."), { recursive: true });
const browser = await chromium.launch({ headless: true });
let web;
let account;
let error = null;
try {
  web = await checkWeb(browser);
  account = await checkAccount(browser);
} catch (caught) {
  error = safeText(caught instanceof Error ? caught.message : String(caught));
} finally {
  await browser.close();
}

const receipt = {
  schema: "issue127-e1-app-ready-v1",
  project: "dlr-i127-e0-141",
  ao_session: "datalinkruntime-141-e0",
  browser: {
    engine: "Chromium",
    transport: "repository-local Playwright fallback",
    in_app_browser: "unavailable: runtime list=[]",
  },
  endpoints: {
    web: { url: BASE_URL, credential_configured: Boolean(ADMIN_TOKEN) },
    account: { url: ACCOUNT_BASE_URL, username: "admin", credential_configured: Boolean(ACCOUNT_PASSWORD) },
  },
  web: web ?? { status: "FAIL" },
  account: account ?? { status: "FAIL" },
  error,
  expected_console_messages: [
    "account login page may probe the protected API before credentials and receive 401 Unauthorized",
  ],
  status: error ? "UNREADY" : "APP_READY",
  human_acceptance: "待人工验收",
};
await writeFile(join(process.cwd(), OUTPUT), `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
console.log(JSON.stringify({
  status: receipt.status,
  web: receipt.web.status,
  account: receipt.account.status,
  human_acceptance: receipt.human_acceptance,
}));
if (error) process.exitCode = 1;
