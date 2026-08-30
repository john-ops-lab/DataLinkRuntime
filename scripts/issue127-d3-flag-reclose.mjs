/*
 * Issue #127 Wave D3 release-flag close/re-open evidence.
 *
 * This probe is intentionally limited to safe API facts and browser-visible
 * state. It never writes a storage key, token, cookie or deployment path to
 * the evidence directory.
 */

import { createRequire } from "node:module";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

const require = createRequire(join(process.cwd(), "package.json"));
const { chromium } = require("playwright");

const BASE_URL = process.env.DLR_D3_BASE_URL ?? "http://127.0.0.1:8923";
const ADMIN_TOKEN = process.env.DLR_D3_ADMIN_TOKEN;
const ADAPTER_ID = Number(process.env.DLR_D3_ADAPTER_ID ?? "1");
const OUTPUT_DIR = process.env.DLR_D3_OUTPUT_DIR ?? "../docs/evidence/issue127-d3";
const STAGE = process.argv[2] ?? "baseline";

if (!ADMIN_TOKEN) throw new Error("DLR_D3_ADMIN_TOKEN is required");
if (!Number.isInteger(ADAPTER_ID) || ADAPTER_ID < 1) throw new Error("DLR_D3_ADAPTER_ID must be positive");
if (!["baseline", "disabled", "reclosed", "compare"].includes(STAGE)) throw new Error(`unknown stage ${STAGE}`);

function safePath(rawUrl) {
  try {
    return new URL(rawUrl).pathname;
  } catch {
    return "<invalid-url>";
  }
}

async function api(path, init = {}) {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${ADMIN_TOKEN}`,
      ...(init.headers ?? {}),
    },
  });
  const text = await response.text();
  let body = null;
  try {
    body = text === "" ? null : JSON.parse(text);
  } catch {
    body = null;
  }
  return { status: response.status, body };
}

function errorCode(body) {
  return body?.detail?.code ?? null;
}

function summarizeConfig(body) {
  return {
    revision: body?.revision ?? null,
    source_type: body?.source_type ?? null,
    artifact_count: Array.isArray(body?.artifacts) ? body.artifacts.length : null,
    artifacts: (body?.artifacts ?? []).map((artifact) => ({
      id: artifact.id,
      ordinal: artifact.ordinal,
      original_filename: artifact.original_filename,
      status: artifact.status,
      sha256: artifact.sha256,
    })),
    retention: body?.retention ?? null,
    valid_for_run: body?.valid_for_run ?? null,
  };
}

function summarizeExecutions(body) {
  return {
    count: Array.isArray(body?.items) ? body.items.length : null,
    ids: (body?.items ?? []).map((item) => item.id),
    statuses: (body?.items ?? []).map((item) => item.status),
  };
}

async function currentFacts() {
  const [capability, config, executions] = await Promise.all([
    api("/api/system/managed-input-capability"),
    api(`/api/adapters/${ADAPTER_ID}/input-config`),
    api(`/api/adapters/${ADAPTER_ID}/executions`),
  ]);
  return {
    capability: { status: capability.status, body: capability.body },
    config: { status: config.status, summary: summarizeConfig(config.body) },
    executions: { status: executions.status, summary: summarizeExecutions(executions.body) },
  };
}

async function disabledApiChecks(configResponse) {
  const form = new FormData();
  form.append("file", new Blob(["D3 disabled flag fixture\n"], { type: "text/plain" }), "d3-disabled-flag.txt");
  const upload = await api(`/api/adapters/${ADAPTER_ID}/input-artifacts`, { method: "POST", body: form });
  const putPayload = {
    expected_revision: configResponse.summary.revision,
    source_type: "managed_files",
    artifact_ids: configResponse.summary.artifacts.map((artifact) => artifact.id),
    retention: configResponse.summary.retention,
  };
  const put = await api(`/api/adapters/${ADAPTER_ID}/input-config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(putPayload),
  });
  return {
    upload: { status: upload.status, code: errorCode(upload.body) },
    input_config_put: { status: put.status, code: errorCode(put.body) },
  };
}

function sourceAdapterButton(page) {
  return page.locator('[data-testid="adapter-item"]')
    .filter({ has: page.locator(".catalog-item-name").filter({ hasText: /^D3 managed input acceptance$/ }) })
    .first();
}

async function browserProbe(stage) {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const telemetry = { console: [], page_errors: [], requests: [], responses: [] };
  page.on("console", (message) => telemetry.console.push({ type: message.type(), text: message.text().slice(0, 240) }));
  page.on("pageerror", (error) => telemetry.page_errors.push(error.message.slice(0, 240)));
  page.on("request", (request) => {
    if (request.url().includes("/api/")) telemetry.requests.push({ method: request.method(), path: safePath(request.url()) });
  });
  page.on("response", (response) => {
    if (response.url().includes("/api/")) telemetry.responses.push({ method: response.request().method(), status: response.status(), path: safePath(response.url()) });
  });
  try {
    await page.goto(`${BASE_URL}/`);
    await page.waitForLoadState("networkidle");
    await page.getByTestId("admin-token-input").fill(ADMIN_TOKEN);
    await page.getByTestId("admin-token-submit").click();
    await page.getByTestId("adapter-catalog").waitFor({ state: "visible" });
    await sourceAdapterButton(page).click();
    await page.getByRole("tab", { name: /运行设置|Runtime settings/ }).click();
    const card = page.getByTestId("task-input-source-managed_files");
    await card.waitFor({ state: "visible" });
    const managedDisabled = await card.getAttribute("aria-disabled");
    if (stage === "disabled" && managedDisabled !== "true") throw new Error("disabled flag did not disable Managed Files card");
    if (stage === "reclosed" && managedDisabled !== "false") throw new Error("reclosed flag did not enable Managed Files card");
    if (stage === "reclosed") {
      await card.click();
      await page.getByTestId("managed-input-editor").waitFor({ state: "visible" });
    }
    await mkdir(join(OUTPUT_DIR, "screenshots"), { recursive: true });
    await page.screenshot({ path: join(OUTPUT_DIR, "screenshots", `flag-${stage}-zh-1280.png`), fullPage: true });
    return { status: "PASS", managed_card_disabled: managedDisabled === "true", telemetry };
  } finally {
    await context.close();
    await browser.close();
  }
}

async function writeStage(stage) {
  const facts = await currentFacts();
  const evidence = {
    stage,
    adapter_id: ADAPTER_ID,
    facts,
    browser: await browserProbe(stage),
    human_acceptance: "待人工验收",
  };
  if (stage === "disabled") {
    evidence.disabled_api = await disabledApiChecks(facts.config);
    if (
      facts.capability.body?.managed_files_enabled !== false ||
      facts.capability.body?.ready !== false ||
      evidence.disabled_api.upload.status !== 422 ||
      evidence.disabled_api.upload.code !== "input_source_not_available" ||
      evidence.disabled_api.input_config_put.status !== 422 ||
      evidence.disabled_api.input_config_put.code !== "input_source_not_available"
    ) {
      evidence.machine_gate = "FAIL";
      throw new Error(`disabled flag checks failed; inspect flag-${stage}.json`);
    }
  } else if (stage === "baseline") {
    if (facts.capability.body?.managed_files_enabled !== true || facts.capability.body?.ready !== true) {
      evidence.machine_gate = "FAIL";
      throw new Error("baseline capability is not true/true");
    }
  } else if (stage === "reclosed") {
    if (facts.capability.body?.managed_files_enabled !== true || facts.capability.body?.ready !== true) {
      evidence.machine_gate = "FAIL";
      throw new Error("reclosed capability is not true/true");
    }
  }
  evidence.machine_gate = "PASS";
  await writeFile(join(OUTPUT_DIR, `flag-${stage}.json`), `${JSON.stringify(evidence, null, 2)}\n`, "utf8");
  console.log(JSON.stringify({ stage, machine_gate: evidence.machine_gate, revision: facts.config.summary.revision, execution_count: facts.executions.summary.count }));
}

function signatures(evidence) {
  return {
    config: evidence.facts.config.summary,
    executions: evidence.facts.executions.summary,
  };
}

async function compare() {
  const [baseline, disabled, reclosed] = await Promise.all([
    readFile(join(OUTPUT_DIR, "flag-baseline.json"), "utf8").then(JSON.parse),
    readFile(join(OUTPUT_DIR, "flag-disabled.json"), "utf8").then(JSON.parse),
    readFile(join(OUTPUT_DIR, "flag-reclosed.json"), "utf8").then(JSON.parse),
  ]);
  const sameAfterReclose = JSON.stringify(signatures(baseline)) === JSON.stringify(signatures(reclosed));
  const closed = disabled.facts.capability.body?.managed_files_enabled === false &&
    disabled.facts.capability.body?.ready === false &&
    disabled.disabled_api.upload.code === "input_source_not_available" &&
    disabled.disabled_api.input_config_put.code === "input_source_not_available";
  const evidence = {
    baseline: signatures(baseline),
    disabled: {
      capability: disabled.facts.capability.body,
      api: disabled.disabled_api,
      browser: { managed_card_disabled: disabled.browser.managed_card_disabled },
    },
    reclosed: signatures(reclosed),
    checks: { wave_b_c_closed: closed, blob_and_execution_history_preserved: sameAfterReclose },
    machine_gate: closed && sameAfterReclose ? "PASS" : "FAIL",
    human_acceptance: "待人工验收",
  };
  await writeFile(join(OUTPUT_DIR, "flag-reclose.json"), `${JSON.stringify(evidence, null, 2)}\n`, "utf8");
  console.log(JSON.stringify({ machine_gate: evidence.machine_gate, wave_b_c_closed: closed, preserved: sameAfterReclose, human_acceptance: evidence.human_acceptance }));
  if (evidence.machine_gate !== "PASS") throw new Error("flag re-close comparison failed");
}

await mkdir(OUTPUT_DIR, { recursive: true });
if (STAGE === "compare") await compare();
else await writeStage(STAGE);
