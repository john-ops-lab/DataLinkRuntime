import { Buffer } from "node:buffer";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page, type Route } from "@playwright/test";

type Locale = "zh-CN" | "en";
type Persona =
  | "superadmin"
  | "account-admin"
  | "owner"
  | "shared-edit"
  | "shared-read"
  | "force-password"
  | "unshared"
  | "empty"
  | "error";

const LOCALES: readonly Locale[] = ["zh-CN", "en"];
const VIEWPORTS = [1280, 1440, 1680, 1920] as const;
const specDir = dirname(fileURLToPath(import.meta.url));
const evidenceRoot = resolve(
  specDir,
  process.env.DLR_WAVE_E_OUTPUT_DIR ?? "../../../docs/ui/m5-10-wave-e",
);
const screenshotDir = resolve(evidenceRoot, "browser");

const taskAdapter = {
  id: 1,
  name: "订单同步适配器 Orders Synchronization Adapter — Long Label",
  description: "Wave E deterministic task fixture with intentionally long Chinese and English copy.",
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

const webhookAdapter = {
  ...taskAdapter,
  id: 2,
  name: "事件接收适配器 Webhook Receiver — Long Label",
  description: "A second deterministic adapter verifies webhook state isolation and long URLs.",
  adapter_type: "webhook",
  latest_version_id: 20,
  running_execution_id: null as number | null,
};

const systemAdapter = {
  ...taskAdapter,
  id: 3,
  name: "系统所有适配器 System-owned Adapter",
  description: "Visible only to superadmin and account administrators.",
  owner_user_id: null,
  owner_username: null,
  latest_version_id: 30,
};

const worker = {
  id: 1,
  name: "runtime-worker-with-an-intentionally-long-display-name",
  status: "online",
  last_heartbeat: "2026-01-01T00:00:00Z",
  capabilities: ["python", "javascript", "java"],
};

const users = Array.from({ length: 12 }, (_, index) => ({
  id: index + 1,
  username: index === 0
    ? "admin-with-a-long-display-name"
    : `fixture-user-${String(index).padStart(2, "0")}-with-long-copy`,
  role: index === 0 ? "admin" : "user",
  enabled: index !== 11,
  must_change_password: false,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
}));

const credentials = [{
  id: 1,
  name: "fixture-token-credential-with-a-long-name",
  type: "token",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
}];

const packageSources = [{
  id: 1,
  name: "Private PyPI source with a very long translated display name",
  kind: "pypi",
  index_url: "https://packages.example.com/repository/python/simple/with/a/long/path",
  is_default: true,
  credential_id: null,
  credential_name: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
}];

const packageDefaults = {
  pypi: { kind: "pypi", name: "Official PyPI", index_url: "https://pypi.org/simple/" },
  npm: { kind: "npm", name: "Official npm", index_url: "https://registry.npmjs.org/" },
  maven: { kind: "maven", name: "Maven Central", index_url: "https://repo1.maven.org/maven2/" },
};

const knowledgeSource = {
  source_id: "ima",
  kind: "ima",
  name: "Tencent ima Knowledge Source",
  endpoint: "https://ima.qq.com",
  enabled: true,
  status: "configured",
  credential_id: null,
  credential_name: null,
  credential_type: null,
  config_source: "environment",
  created_at: null,
  updated_at: null,
};

const schedule = {
  adapter_id: 1,
  enabled: false,
  cron: "*/5 * * * *",
  timezone: "Asia/Shanghai",
  input: { fixture: true },
  next_run_at: null,
  updated_at: "2026-01-01T00:00:00Z",
};

const webhook = {
  adapter_id: 2,
  enabled: false,
  public_id: "wave-e-webhook-long-public-id",
  hook_path: "/api/hooks/wave-e-webhook-long-public-id",
  credential_id: 1,
  credential_name: credentials[0].name,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const attachmentCapabilities = {
  limits: {
    max_attachments: 5,
    max_file_bytes: 2_000_000,
    max_total_bytes: 5_000_000,
  },
  supported_content_types: ["text/plain", "text/markdown", "text/x-python", "application/json"],
};

const versionFor = (adapterId: number) => ({
  id: adapterId === 1 ? 10 : adapterId === 2 ? 20 : 30,
  adapter_id: adapterId,
  seq: adapterId === 1 ? 7 : adapterId === 2 ? 3 : 1,
  created_at: "2026-01-01T00:00:00Z",
});

function adapterFor(adapterId: number, accessLevel: "admin" | "owner" | "edit" | "read") {
  const base = adapterId === 1 ? taskAdapter : adapterId === 2 ? webhookAdapter : systemAdapter;
  return { ...base, access_level: accessLevel };
}

function detailFor(adapterId: number) {
  const version = versionFor(adapterId);
  return {
    ...version,
    code: adapterId === 2
      ? "def receive(payload):\n    return {'webhook': True, 'payload': payload}\n"
      : "def transform(payload):\n    return {'fixture': True, 'payload': payload}\n",
    requirements: "requests==2.32.3\n",
    runtime_config: { fixture: true, adapter_id: adapterId },
  };
}

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
  stdout: "fixture stdout line 1\nfixture stdout line 2\n这是浏览器可见的已脱敏日志。\n",
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
  version_seq: versionFor(1).seq,
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

interface BrowserRecord {
  locale: Locale;
  width: number;
  persona: Persona;
  stage: string;
  screenshot: string;
  console_errors: string[];
  expected_console_errors: string[];
  page_errors: string[];
  unknown_requests: string[];
  overflow: { inner_width: number; document_scroll_width: number; body_scroll_width: number };
  visible_states: Record<string, boolean>;
}

const records: BrowserRecord[] = [];
let browserVersion = "unknown";

function jsonBody(body: unknown): string {
  return JSON.stringify(body);
}

async function fulfillJson(route: Route, body: unknown, status = 200, headers?: Record<string, string>) {
  await route.fulfill({ status, contentType: "application/json", headers, body: jsonBody(body) });
}

function errorBody(code: string, message: string) {
  return { detail: { code, message } };
}

function isAccountPersona(persona: Persona): boolean {
  return persona !== "superadmin" && persona !== "empty" && persona !== "error";
}

function principalFor(persona: Persona) {
  if (persona === "account-admin") {
    return { id: 1, username: "admin-with-a-long-display-name", role: "admin" as const, enabled: true, must_change_password: false };
  }
  const id = persona === "owner" ? 42 : persona === "shared-edit" ? 7 : persona === "shared-read" ? 8 : 9;
  return { id, username: `${persona}-user-with-a-long-display-name`, role: "user" as const, enabled: true, must_change_password: persona === "force-password" };
}

function accessFor(persona: Persona, adapterId: number): "admin" | "owner" | "edit" | "read" {
  if (persona === "superadmin" || persona === "account-admin") return "admin";
  if (persona === "owner" && adapterId === 1) return "owner";
  if (persona === "shared-edit" && adapterId === 2) return "edit";
  if (persona === "shared-read" && adapterId === 2) return "read";
  return "read";
}

function visibleAdapters(persona: Persona) {
  switch (persona) {
    case "superadmin":
    case "account-admin":
      return [1, 2, 3];
    case "owner":
      return [1];
    case "shared-edit":
    case "shared-read":
      return [2];
    default:
      return [];
  }
}

function captureDiagnostics(page: Page, expectedStatuses: readonly number[] = []) {
  const consoleErrors: string[] = [];
  const expectedConsoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const text = message.text();
    if (expectedStatuses.some((status) => text.includes(`status of ${status}`))) {
      expectedConsoleErrors.push(text);
    } else {
      consoleErrors.push(text);
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.stack ?? error.message));
  return { consoleErrors, expectedConsoleErrors, pageErrors };
}

async function installRoutes(page: Page, persona: Persona, locale: Locale) {
  let accountLoggedIn = false;
  const unknownRequests: string[] = [];
  const diagnostics = captureDiagnostics(
    page,
    isAccountPersona(persona) ? [401] : persona === "error" ? [503] : [],
  );
  let currentSchedule = { ...schedule };
  let currentWebhook = { ...webhook };

  await page.route("**/entry-mode.js", async (route) => {
    await route.fulfill({
      contentType: "application/javascript",
      body: `window.__DLR_ENTRY_MODE__ = "${isAccountPersona(persona) ? "account" : "token"}";`,
    });
  });

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === "/api/locale" && method === "GET") return fulfillJson(route, { locale });
    if (path === "/api/locale" && method === "PUT") return fulfillJson(route, { locale: request.postDataJSON()?.locale ?? locale });
    if (path === "/api/health" && method === "GET") return fulfillJson(route, { status: "ok", database: true });
    if (path === "/api/workers" && method === "GET") return fulfillJson(route, [worker]);

    if (path === "/api/auth/admin/verify" && method === "GET" && (persona === "superadmin" || persona === "empty")) {
      return fulfillJson(route, { status: "ok" });
    }
    if (path === "/api/auth/admin/verify" && method === "GET" && persona === "error") {
      return fulfillJson(route, errorBody("admin_verify_failed", "Wave E fixture token verification failure"), 503);
    }
    if (path === "/api/auth/account/csrf" && method === "GET" && isAccountPersona(persona)) {
      return fulfillJson(route, { status: "ok" }, 200, { "set-cookie": "dlr_account_csrf=wave-e-csrf; Path=/" });
    }
    if (path === "/api/auth/account/me" && method === "GET" && isAccountPersona(persona)) {
      return accountLoggedIn
        ? fulfillJson(route, { principal: principalFor(persona) })
        : fulfillJson(route, errorBody("account_session_required", "Account Session is required"), 401);
    }
    if (path === "/api/auth/account/login" && method === "POST" && isAccountPersona(persona)) {
      accountLoggedIn = true;
      return fulfillJson(route, { principal: principalFor(persona) });
    }
    if (path === "/api/auth/account/logout" && method === "POST" && isAccountPersona(persona)) {
      accountLoggedIn = false;
      return fulfillJson(route, { status: "ok" });
    }
    if (path === "/api/auth/account/change-password" && method === "POST" && isAccountPersona(persona)) {
      return fulfillJson(route, { status: "ok" });
    }

    if (path === "/api/adapters" && method === "GET") {
      if (persona === "error") return fulfillJson(route, errorBody("adapter_list_failed", "Wave E fixture list failure"), 503);
      if (persona === "empty") return fulfillJson(route, []);
      return fulfillJson(route, visibleAdapters(persona).map((id) => adapterFor(id, accessFor(persona, id))));
    }
    if (path === "/api/adapters" && method === "POST") {
      return fulfillJson(route, adapterFor(99, accessFor(persona, 1)), 201);
    }
    const adapterMatch = path.match(/^\/api\/adapters\/(\d+)$/);
    if (adapterMatch && method === "GET") {
      const adapterId = Number(adapterMatch[1]);
      if (!visibleAdapters(persona).includes(adapterId)) {
        return fulfillJson(route, errorBody("adapter_not_found", "Adapter not found"), 404);
      }
      return fulfillJson(route, adapterFor(adapterId, accessFor(persona, adapterId)));
    }
    if (adapterMatch && method === "PATCH") {
      const adapterId = Number(adapterMatch[1]);
      return fulfillJson(route, { ...adapterFor(adapterId, accessFor(persona, adapterId)), ...(request.postDataJSON() ?? {}) });
    }
    if (adapterMatch && method === "DELETE") return fulfillJson(route, undefined, 204);

    const versionsMatch = path.match(/^\/api\/adapters\/(\d+)\/versions$/);
    if (versionsMatch && method === "GET") return fulfillJson(route, [versionFor(Number(versionsMatch[1]))]);
    if (versionsMatch && method === "POST") return fulfillJson(route, detailFor(Number(versionsMatch[1])), 201);
    const versionDetailMatch = path.match(/^\/api\/adapters\/(\d+)\/versions\/(\d+)$/);
    if (versionDetailMatch && method === "GET") return fulfillJson(route, detailFor(Number(versionDetailMatch[1])));
    const cloneMatch = path.match(/^\/api\/adapters\/(\d+)\/clone$/);
    if (cloneMatch && method === "POST") return fulfillJson(route, adapterFor(99, accessFor(persona, 1)), 201);

    const scheduleMatch = path.match(/^\/api\/adapters\/(\d+)\/schedule$/);
    if (scheduleMatch && method === "GET") return fulfillJson(route, currentSchedule);
    if (scheduleMatch && method === "PUT") {
      currentSchedule = { ...currentSchedule, ...(request.postDataJSON() ?? {}) };
      return fulfillJson(route, currentSchedule);
    }
    const webhookMatch = path.match(/^\/api\/adapters\/(\d+)\/webhook$/);
    if (webhookMatch && method === "GET") return fulfillJson(route, currentWebhook);
    if (webhookMatch && method === "PUT") {
      currentWebhook = { ...currentWebhook, ...(request.postDataJSON() ?? {}) };
      return fulfillJson(route, currentWebhook);
    }

    const bindingsMatch = path.match(/^\/api\/adapters\/(\d+)\/credential-bindings$/);
    if (bindingsMatch && method === "GET") return fulfillJson(route, []);
    if (bindingsMatch && method === "PUT") return fulfillJson(route, []);
    const optionsMatch = path.match(/^\/api\/adapters\/(\d+)\/credential-options$/);
    if (optionsMatch && method === "GET") return fulfillJson(route, credentials);

    const permissionsMatch = path.match(/^\/api\/adapters\/(\d+)\/permissions$/);
    if (permissionsMatch && method === "GET") {
      return fulfillJson(route, [{ user_id: 7, username: "shared-edit-user", enabled: true, permission: "edit" }]);
    }
    const candidatesMatch = path.match(/^\/api\/adapters\/(\d+)\/permission-candidates$/);
    if (candidatesMatch && method === "GET") {
      return fulfillJson(route, [{ id: 8, username: "shared-read-user", role: "user", enabled: true }]);
    }
    const permissionItemMatch = path.match(/^\/api\/adapters\/(\d+)\/permissions\/(\d+)$/);
    if (permissionItemMatch && (method === "PUT" || method === "DELETE")) {
      return fulfillJson(route, method === "PUT" ? { user_id: Number(permissionItemMatch[2]), username: "fixture-user", enabled: true, permission: "read" } : undefined, method === "DELETE" ? 204 : 200);
    }

    if (path === "/api/adapters/1/executions" && method === "POST") return fulfillJson(route, execution, 201);
    if (path === "/api/adapters/1/executions" && method === "GET") return fulfillJson(route, { items: [executionSummary], next_before_id: null });
    if (path === "/api/adapters/2/executions" && method === "GET") return fulfillJson(route, { items: [], next_before_id: null });
    if (path === "/api/executions/5" && method === "GET") return fulfillJson(route, completedExecution);
    if (path === "/api/executions/5/events" && method === "GET") {
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: `event: execution\ndata: ${jsonBody(completedExecution)}\n\n` });
    }
    if (path === "/api/executions/5/cancel" && method === "POST") return fulfillJson(route, { ...completedExecution, status: "cancelled" });

    if (path === "/api/users" && method === "GET" && (persona === "superadmin" || persona === "account-admin")) return fulfillJson(route, users);
    if (path === "/api/users" && method === "POST" && (persona === "superadmin" || persona === "account-admin")) return fulfillJson(route, { ...users[1], id: 99, username: "created-fixture-user" }, 201);
    const userMatch = path.match(/^\/api\/users\/(\d+)$/);
    if (userMatch && method === "PATCH" && (persona === "superadmin" || persona === "account-admin" || (isAccountPersona(persona) && Number(userMatch[1]) === principalFor(persona).id))) {
      return fulfillJson(route, { ...users[Number(userMatch[1]) - 1] ?? principalFor(persona), ...(request.postDataJSON() ?? {}) });
    }
    if (path.match(/^\/api\/users\/\d+\/reset-password$/) && method === "POST" && (persona === "superadmin" || persona === "account-admin")) return fulfillJson(route, users[1]);

    if (path === "/api/credentials" && method === "GET" && (persona === "superadmin" || persona === "account-admin")) return fulfillJson(route, credentials);
    if (path === "/api/credentials" && method === "POST" && (persona === "superadmin" || persona === "account-admin")) return fulfillJson(route, credentials[0], 201);
    if (path.match(/^\/api\/credentials\/\d+$/) && (method === "PATCH" || method === "DELETE")) return fulfillJson(route, method === "DELETE" ? undefined : credentials[0], method === "DELETE" ? 204 : 200);
    if (path === "/api/package-sources" && method === "GET" && (persona === "superadmin" || persona === "account-admin")) return fulfillJson(route, packageSources);
    if (path === "/api/package-sources" && method === "POST" && (persona === "superadmin" || persona === "account-admin")) return fulfillJson(route, packageSources[0], 201);
    if (path === "/api/package-sources/defaults" && method === "GET" && (persona === "superadmin" || persona === "account-admin")) return fulfillJson(route, packageDefaults);
    if (path.match(/^\/api\/package-sources\/\d+$/) && (method === "PATCH" || method === "DELETE")) return fulfillJson(route, method === "DELETE" ? undefined : packageSources[0], method === "DELETE" ? 204 : 200);
    if (path.match(/^\/api\/package-sources\/\d+\/test$/) && method === "POST") return fulfillJson(route, { ok: true, status_code: 200, error: null });
    if (path.match(/^\/api\/package-sources\/defaults\/(pypi|npm|maven)$/) && method === "POST") return fulfillJson(route, packageSources[0]);
    if (path === "/api/knowledge-sources" && method === "GET") return fulfillJson(route, [knowledgeSource]);
    if (path === "/api/knowledge-sources/ima" && method === "GET") return fulfillJson(route, knowledgeSource);
    if (path === "/api/knowledge-sources/ima" && method === "PUT") return fulfillJson(route, knowledgeSource);
    if (path === "/api/knowledge-sources/ima/test" && method === "POST") return fulfillJson(route, { ok: true, status: "connected", error_code: null, message: "Fixture connected", knowledge_bases: [{ id: "fixture", name: "Fixture KB", status: "accessible" }] });
    if (path === "/api/knowledge-sources/ima/validate" && method === "POST") return fulfillJson(route, { ok: true, status: "connected", error_code: null, message: "Fixture validated", knowledge_bases: [] });
    if (path === "/api/knowledge-sources/ima/knowledge-bases" && method === "GET") return fulfillJson(route, [{ id: "fixture", name: "Fixture KB", status: "accessible" }]);
    if (path === "/api/ai/settings" && method === "GET") return fulfillJson(route, null);
    if (path === "/api/ai/settings" && method === "PUT") return fulfillJson(route, { id: 1, provider: "custom_openai_compatible", base_url: "https://example.invalid", model: "fixture-model", credential_id: null, credential_name: null, reasoning_mode: "default", reasoning_effort: null, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" });
    if (path === "/api/ai/settings/test" && method === "POST") return fulfillJson(route, { ok: true, message: "Fixture connection succeeded", models: ["fixture-model"] });
    if (path === "/api/ai/models/refresh" && method === "POST") return fulfillJson(route, { models: ["fixture-model"] });
    if (path === "/api/ai/attachment-capabilities" && method === "GET") return fulfillJson(route, attachmentCapabilities);
    if (path.match(/^\/api\/adapters\/\d+\/ai\/assist$/) && method === "POST") {
      if (persona === "error") return fulfillJson(route, errorBody("ai_provider_unavailable", "Fixture AI provider is unavailable"), 503);
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 120));
      return fulfillJson(route, {
        message: "# Fixture response\n\nDeterministic Markdown with a long line that must wrap safely.\n\n```python\nreturn {'fixture': True}\n```",
        provider: "custom_openai_compatible",
        model: "fixture-model",
        candidate: { summary: "Fixture candidate update", code: "def transform(payload):\n    return {'fixture': True}\n", required_secret_keys: [] },
        tool_calls: [{ tool_name: "search_knowledge", status: "success", args_summary: "fixture query", result_summary: "fixture result", error_code: null }],
      });
    }

    const requestKey = `${method} ${path}${url.search}`;
    unknownRequests.push(requestKey);
    await fulfillJson(route, errorBody("wave_e_unhandled_request", requestKey), 404);
  });

  return { unknownRequests, diagnostics };
}

async function measureOverflow(page: Page) {
  return page.evaluate(() => ({
    inner_width: window.innerWidth,
    document_scroll_width: document.documentElement.scrollWidth,
    body_scroll_width: document.body.scrollWidth,
  }));
}

async function screenshot(page: Page, locale: Locale, width: number, persona: Persona, stage: string) {
  mkdirSync(screenshotDir, { recursive: true });
  const safeStage = stage.replace(/[^a-z0-9-]+/gi, "-").replace(/-+$/, "");
  const name = `${locale}-${width}-${persona}-${safeStage}.png`;
  if (stage === "candidate-diff") {
    await expect(page.locator(".ant-modal-wrap:visible .ant-modal-content")).toBeVisible();
    await page.waitForTimeout(200);
  }
  await page.screenshot({ path: resolve(screenshotDir, name), fullPage: true, animations: "disabled" });
  return `docs/ui/m5-10-wave-e/browser/${name}`;
}

async function finishRecord(
  page: Page,
  locale: Locale,
  width: number,
  persona: Persona,
  stage: string,
  diagnostics: ReturnType<typeof captureDiagnostics>,
  unknownRequests: string[],
  visibleStates: Record<string, boolean>,
) {
  const overflow = await measureOverflow(page);
  expect(overflow.document_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(overflow.body_scroll_width).toBeLessThanOrEqual(overflow.inner_width);
  expect(unknownRequests, `${persona}/${stage} unknown requests`).toEqual([]);
  expect(diagnostics.consoleErrors, `${persona}/${stage} console errors`).toEqual([]);
  expect(diagnostics.pageErrors, `${persona}/${stage} page errors`).toEqual([]);
  records.push({
    locale,
    width,
    persona,
    stage,
    screenshot: await screenshot(page, locale, width, persona, stage),
    console_errors: [...diagnostics.consoleErrors],
    expected_console_errors: [...diagnostics.expectedConsoleErrors],
    page_errors: [...diagnostics.pageErrors],
    unknown_requests: [...unknownRequests],
    overflow,
    visible_states: visibleStates,
  });
}

async function login(page: Page, persona: Persona) {
  await page.goto("/");
  if (persona === "superadmin") {
    await expect(page.getByTestId("admin-token-input")).toBeVisible();
    await page.getByTestId("admin-token-input").fill("fixture-token");
    await page.getByTestId("admin-token-submit").click();
  } else {
    await expect(page.getByTestId("account-username-input")).toBeVisible();
    await page.getByTestId("account-username-input").fill("fixture-user");
    await page.getByTestId("account-password-input").fill("fixture-password");
    await page.getByTestId("account-login-submit").click();
  }
  await expect(page.getByTestId("adapter-catalog")).toBeVisible();
}

async function openAdapter(page: Page, adapterId: number) {
  await page.getByTestId("adapter-item").filter({ hasText: adapterId === 1 ? "订单同步适配器" : "事件接收适配器" }).click();
  await expect(page.getByTestId("workbench-header")).toBeVisible();
  await expect(page.getByTestId("editor-main")).toBeVisible();
}

async function auditShellCatalogAndKeyboard(page: Page, locale: Locale, expectedSearchMatches = 2) {
  await expect(page.getByTestId("app-header")).toBeVisible();
  await expect(page.locator(".catalog-title")).toBeVisible();
  await expect(page.getByTestId("adapter-search")).toHaveAccessibleName(locale === "zh-CN" ? "搜索适配器" : "Search Adapters");
  await page.getByTestId("adapter-search").fill("Long Label");
  await expect(page.getByTestId("adapter-item")).toHaveCount(expectedSearchMatches);
  await page.getByTestId("adapter-search").fill("");
  await page.getByTestId("adapter-type-filter").click();
  await page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option").first().press("Escape");
  await page.getByTestId("adapter-search").focus();
  const focusedLabels: string[] = [];
  for (let index = 0; index < 8; index += 1) {
    await page.keyboard.press("Tab");
    focusedLabels.push(await page.evaluate(() => document.activeElement?.getAttribute("aria-label") ?? document.activeElement?.getAttribute("data-testid") ?? ""));
  }
  expect(focusedLabels.some((label) => label.includes("更多操作") || label.includes("More actions"))).toBe(true);
  await page.getByTestId("adapter-type-filter").focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByTestId("adapter-search").focus();
  expect(await page.evaluate(() => document.activeElement?.getAttribute("data-testid"))).toBe("adapter-search");
}

async function auditAdminSettings(page: Page, locale: Locale, width: number, persona: Persona, unknownRequests: string[], diagnostics: ReturnType<typeof captureDiagnostics>) {
  await page.getByTestId("user-menu").click();
  await page.getByTestId("system-settings").click();
  await page.getByRole("menuitem", { name: locale === "zh-CN" ? "凭据" : "Credentials" }).click();
  await expect(page.getByTestId("credentials-panel")).toBeVisible();
  await expect(page.getByTestId("credential-row")).toBeVisible();
  await page.getByTestId("new-credential").click();
  await expect(page.getByTestId("credential-name")).toBeVisible();
  await page.getByTestId("credential-name").fill("fixture-credential-name");
  await page.locator(".ant-modal-root").last().locator(".ant-modal-close").click();

  page.once("dialog", async (dialog) => await dialog.accept());
  await page.getByRole("menuitem", { name: locale === "zh-CN" ? "依赖源" : "Package sources" }).click();
  await expect(page.getByTestId("package-sources-panel")).toBeVisible();
  await expect(page.getByTestId("package-source-row")).toBeVisible();
  await page.getByTestId("new-package-source").click();
  await expect(page.getByTestId("package-source-url")).toBeVisible();
  await page.getByTestId("package-source-url").fill("https://packages.example.com/a/very/long/repository/path/simple/");
  await page.locator(".ant-modal-root").last().locator(".ant-modal-close").click();

  page.once("dialog", async (dialog) => await dialog.accept());
  await page.getByRole("menuitem", { name: locale === "zh-CN" ? "知识库" : "Knowledge base" }).click();
  await expect(page.getByTestId("knowledge-source-summary")).toBeVisible();
  await expect(page.getByTestId("knowledge-source-endpoint")).toHaveText("https://ima.qq.com");
  await page.getByRole("menuitem", { name: locale === "zh-CN" ? "AI 模型" : "AI model" }).click();
  await expect(page.getByTestId("ai-model-settings-panel")).toBeVisible();
  await finishRecord(page, locale, width, persona, "system-settings-ai", diagnostics, unknownRequests, {
    system_settings_drawer: true,
    credentials_form: true,
    package_sources_form: true,
    knowledge_source: true,
    ai_model_form: true,
    drawer_overflow: true,
  });
  await page.getByTestId("settings-back").click();

  await page.getByTestId("user-menu").click();
  await page.getByTestId("user-management").click();
  await expect(page.getByTestId("user-management-drawer")).toBeVisible();
  await expect(page.locator(".ant-pagination")).toBeVisible();
  await page.getByRole("textbox", { name: locale === "zh-CN" ? "搜索账号" : "Search users" }).fill("fixture-user-01");
  await expect(page.getByTestId("user-reset-2")).toBeVisible();
  await page.getByRole("textbox", { name: locale === "zh-CN" ? "搜索账号" : "Search users" }).fill("");
  await expect(page.getByTestId("users-bulk-enable")).toBeDisabled();
  await page.locator(".ant-table-row .ant-checkbox-input").first().check();
  await expect(page.getByTestId("users-bulk-enable")).toBeEnabled();
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByTestId("users-bulk-disable").click();
  await expect(page.getByRole("status")).toBeVisible();
  await page.getByTestId("user-create-username").fill("created-fixture-user-with-long-label");
  await page.getByTestId("user-create-password").fill("fixture-password-123");
  await page.getByTestId("user-create-submit").click();
  await expect(page.getByRole("status")).toBeVisible();
  expect(await page.locator("body").textContent()).not.toContain("fixture-password-123");
  await finishRecord(page, locale, width, persona, "user-management", diagnostics, unknownRequests, {
    user_management_drawer: true,
    user_search: true,
    user_pagination: true,
    user_bulk_action: true,
    user_create_form: true,
    password_not_visible: true,
    drawer_overflow: true,
  });
  await page.locator(".ant-drawer-open").last().locator(".ant-drawer-close").click();
}

async function auditTaskAndAi(page: Page, locale: Locale, width: number, persona: Persona, unknownRequests: string[], diagnostics: ReturnType<typeof captureDiagnostics>) {
  const adapterId = persona === "shared-edit" || persona === "shared-read" ? 2 : 1;
  await openAdapter(page, adapterId);
  const canEdit = persona === "superadmin" || persona === "account-admin" || persona === "owner" || persona === "shared-edit";
  await expect(page.getByRole("region", { name: locale === "zh-CN" ? "适配器代码编辑器" : "Adapter code editor" })).toBeVisible();
  await expect(page.getByRole("toolbar", { name: locale === "zh-CN" ? "代码编辑器工具栏" : "Code editor toolbar" })).toBeVisible();
  if (adapterId === 2) {
    if (canEdit) {
      await expect(page.getByTestId("open-ai-assistant")).toBeVisible();
      await page.getByTestId("open-ai-assistant").click();
      await page.getByTestId("ai-message-input").fill("Inspect this shared-edit fixture without changing ACL state.");
      await page.getByTestId("ai-send").click();
      await expect(page.getByTestId("ai-candidate")).toBeVisible();
      await page.getByTestId("close-ai-assistant").click();
    } else {
      await expect(page.getByTestId("adapter-read-only-notice")).toBeVisible();
      await expect(page.getByTestId("open-ai-assistant")).toHaveCount(0);
      await expect(page.getByTestId("adapter-read-only")).toBeVisible();
    }
    await finishRecord(page, locale, width, persona, canEdit ? "shared-edit" : "shared-read", diagnostics, unknownRequests, {
      shared_adapter: true,
      permission_denied: !canEdit,
      ai_enabled_for_edit: canEdit,
      ai_hidden_for_read: !canEdit,
      webhook_state_surface: true,
    });
    return;
  }
  if (canEdit) {
    await expect(page.getByTestId("open-ai-assistant")).toBeVisible();
    await page.getByTestId("header-task-run-once").click();
    await expect(page.getByTestId("live-log")).toBeVisible();
    await page.getByTestId("live-log").selectText();
    await expect(page.getByTestId("live-log-add-context")).toBeEnabled();
    await page.getByTestId("live-log-add-context").click();
    await page.getByTestId("live-log-maximize").click();
    await expect(page.getByTestId("live-log-restore")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("live-log-maximize")).toBeVisible();
    await page.getByTestId("close-ai-assistant").click();
    await page.getByRole("tab", { name: locale === "zh-CN" ? "执行记录" : "Executions" }).click();
    await expect(page.getByTestId("history-row")).toBeVisible();
    await page.getByTestId("history-row").click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("dialog").getByRole("tab", { name: locale === "zh-CN" ? "执行日志" : "Execution log" }).click();
    await expect(page.getByTestId("detail-log")).toBeVisible();
    await page.getByTestId("detail-log-maximize").click();
    await expect(page.getByTestId("detail-log-restore")).toBeVisible();
    await page.getByTestId("detail-log-restore").click();
    await page.getByRole("dialog").getByRole("button", { name: locale === "zh-CN" ? "关闭" : "Close" }).click();

    await page.getByRole("tab", { name: locale === "zh-CN" ? "编辑" : "Edit" }).click();
    await page.getByTestId("open-ai-assistant").click();
    const input = page.getByTestId("ai-message-input");
    await input.fill("Keep this deterministic draft while the assistant changes layout.");
    await page.getByTestId("maximize-ai-assistant").click();
    await expect(page.getByTestId("restore-ai-assistant")).toBeVisible();
    await expect(input).toHaveValue("Keep this deterministic draft while the assistant changes layout.");
    await page.getByTestId("restore-ai-assistant").click();
    await page.getByTestId("ai-attachment-input").setInputFiles({ name: "fixture-context.txt", mimeType: "text/plain", buffer: Buffer.from("deterministic attachment content", "utf8") });
    await expect(page.getByTestId("ai-attachment-item")).toBeVisible();
    await input.fill("Generate a deterministic candidate from the selected context.");
    await page.getByTestId("ai-send").click();
    await expect(page.getByTestId("ai-loading")).toBeVisible();
    await expect(page.getByTestId("ai-message-assistant")).toBeVisible();
    await expect(page.getByTestId("ai-tool-call")).toBeVisible();
    await expect(page.getByTestId("ai-candidate")).toBeVisible();
    await expect(page.getByTestId("ai-code-copy")).toBeVisible();
    await page.getByTestId("ai-view-diff").click();
    await expect(page.getByTestId("version-diff")).toBeVisible();
    await finishRecord(page, locale, width, persona, "candidate-diff", diagnostics, unknownRequests, {
      ai_candidate_diff: true,
      diff_code_only: true,
      diff_modal_overflow: true,
    });
    await page.getByTestId("diff-apply-candidate").click();
    await expect(page.getByTestId("ai-candidate-applied")).toBeVisible();
    // Apply closes the Candidate Diff by contract. Older Modal child trees can
    // leave the close control mounted, so close it only when it is still present.
    const diffClose = page.getByTestId("diff-close");
    if (await diffClose.count()) await diffClose.click({ force: true });
    await finishRecord(page, locale, width, persona, "task-ai", diagnostics, unknownRequests, {
      task_workbench: true,
      monaco_accessible: true,
      live_log: true,
      log_maximize_restore: true,
      history_detail: true,
      history_log_maximize_restore: true,
      ai_markdown_code: true,
      ai_candidate: true,
      ai_diff_apply: true,
      ai_attachment: true,
    });
  } else {
    await expect(page.getByTestId("adapter-read-only-notice")).toBeVisible();
    await expect(page.getByTestId("open-ai-assistant")).toHaveCount(0);
    await expect(page.getByTestId("adapter-read-only")).toBeVisible();
    await expect(page.getByTestId("editor-main")).toHaveAttribute("aria-label", locale === "zh-CN" ? "适配器代码编辑器" : "Adapter code editor");
    await finishRecord(page, locale, width, persona, "task-read-only", diagnostics, unknownRequests, {
      permission_denied: true,
      disabled_editor: true,
      ai_hidden: true,
    });
  }
}

async function auditWebhook(page: Page, locale: Locale, width: number, persona: Persona, unknownRequests: string[], diagnostics: ReturnType<typeof captureDiagnostics>) {
  if (await page.getByTestId("close-ai-assistant").isVisible().catch(() => false)) {
    await page.getByTestId("close-ai-assistant").click();
  }
  page.once("dialog", (dialog) => void dialog.accept());
  await openAdapter(page, 2);
  await page.waitForTimeout(200);
  await page.getByRole("tab", { name: locale === "zh-CN" ? "运行设置" : "Runtime settings" }).click();
  await expect(page.getByTestId("webhook-run-settings")).toBeVisible();
  await expect(page.getByTestId("webhook-url")).toBeVisible();
  await expect(page.getByTestId("webhook-credential")).toBeVisible();
  await expect(page.getByTestId("webhook-start-blocked")).toHaveCount(0);
  await finishRecord(page, locale, width, persona, "webhook-runtime", diagnostics, unknownRequests, {
    webhook_runtime_settings: true,
    webhook_url: true,
    webhook_credential: true,
    modal_drawer_overflow: true,
  });
  await page.getByTestId("header-webhook-toggle").click();
  await expect(page.getByTestId("header-webhook-toggle")).toBeVisible();
  await page.getByRole("tab", { name: locale === "zh-CN" ? "调用记录" : "Calls" }).click();
  await expect(page.locator(".history-scroll .ant-empty")).toBeVisible();
  await finishRecord(page, locale, width, persona, "webhook", diagnostics, unknownRequests, {
    webhook_workbench: true,
    webhook_url: true,
    webhook_credential: true,
    webhook_history_empty: true,
    adapter_switch_state_isolation: true,
  });
}

async function auditOwnerSettings(page: Page, locale: Locale, width: number, unknownRequests: string[], diagnostics: ReturnType<typeof captureDiagnostics>) {
  await openAdapter(page, 1);
  await page.getByTestId("adapter-item-menu").first().click();
  await page.getByRole("menuitem", { name: locale === "zh-CN" ? "设置" : "Settings" }).click();
  await expect(page.getByTestId("open-adapter-permissions")).toBeVisible();
  await page.getByTestId("open-adapter-permissions").click();
  await expect(page.getByTestId("adapter-permissions")).toBeVisible();
  await expect(page.getByTestId("adapter-permission-row")).toBeVisible();
  await expect(page.getByTestId("adapter-permission-account")).toBeVisible();
  await finishRecord(page, locale, width, "owner", "owner-settings", diagnostics, unknownRequests, {
    owner_acl: true,
    permission_list: true,
    grant_controls: true,
  });
  await page.locator(".ant-drawer-open").last().locator(".ant-drawer-close").click();
}

async function auditAccountProfile(page: Page, locale: Locale, width: number, persona: Persona, unknownRequests: string[], diagnostics: ReturnType<typeof captureDiagnostics>) {
  if (await page.getByTestId("close-ai-assistant").isVisible().catch(() => false)) {
    await page.getByTestId("close-ai-assistant").click();
  }
  await page.getByTestId("user-menu").click();
  await page.getByTestId("account-profile").click();
  await expect(page.getByTestId("account-profile-username")).toBeVisible();
  await expect(page.getByTestId("account-user-password-submit")).toBeVisible();
  await page.getByTestId("account-profile-username").fill(`${persona}-updated-display-name`);
  await page.getByTestId("account-profile-save").click();
  await expect(page.locator(".ant-drawer-open .settings-panel-success")).toBeVisible();
  await finishRecord(page, locale, width, persona, "profile", diagnostics, unknownRequests, {
    profile_drawer: true,
    profile_form: true,
    password_form: true,
  });
  await page.locator(".ant-drawer-open").last().locator(".ant-drawer-close").click();
}

async function auditErrorState(page: Page, locale: Locale, persona: Persona) {
  await page.goto("/");
  if (persona === "error") {
    await page.getByTestId("admin-token-input").fill("fixture-token");
    await page.getByTestId("admin-token-submit").click();
    await expect(page.getByTestId("login-error")).toBeVisible();
    await expect(page.getByTestId("login-error")).toContainText(locale === "zh-CN" ? "登录失败" : "Login failed");
  }
}

test.beforeAll(async ({ browser }) => {
  browserVersion = browser.browserType().name();
  mkdirSync(screenshotDir, { recursive: true });
});

test.afterAll(() => {
  records.sort((a, b) => a.locale.localeCompare(b.locale) || a.width - b.width || a.persona.localeCompare(b.persona) || a.stage.localeCompare(b.stage));
  mkdirSync(evidenceRoot, { recursive: true });
  writeFileSync(resolve(evidenceRoot, "browser-report.json"), `${JSON.stringify({
    schema_version: 1,
    product: "DataLinkRuntime",
    wave: "M5.10 Wave E",
    scope: "complete reachable-page real-browser audit after Waves A-D",
    antd: "5.29.3",
    pro_components: "2.8.10",
    playwright: "1.62.1",
    browser: browserVersion,
    viewport_widths: VIEWPORTS,
    viewport_height: 900,
    locales: LOCALES,
    deterministic_fixtures: true,
    real_credentials: false,
    records,
  }, null, 2)}\n`, "utf8");
});

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`${locale} ${width}px Wave E superadmin reachable-page audit`, async ({ browser }) => {
      const context = await browser.newContext({ locale, viewport: { width, height: 900 } });
      const page = await context.newPage();
      const fixture = await installRoutes(page, "superadmin", locale);
      await login(page, "superadmin");
      await auditShellCatalogAndKeyboard(page, locale, 2);
      await finishRecord(page, locale, width, "superadmin", "shell-catalog", fixture.diagnostics, fixture.unknownRequests, {
        shell: true,
        navigation: true,
        catalog_search: true,
        catalog_select_filter: true,
        keyboard_focus: true,
        top_bar: true,
      });
      await auditAdminSettings(page, locale, width, "superadmin", fixture.unknownRequests, fixture.diagnostics);
      await auditTaskAndAi(page, locale, width, "superadmin", fixture.unknownRequests, fixture.diagnostics);
      await auditWebhook(page, locale, width, "superadmin", fixture.unknownRequests, fixture.diagnostics);
      await context.close();
    });
  }
}

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`${locale} ${width}px Wave E account roles and ACL audit`, async ({ browser }) => {
      const context = await browser.newContext({ locale, viewport: { width, height: 900 } });
      for (const persona of ["account-admin", "owner", "shared-edit", "shared-read"] as const) {
        const page = await context.newPage();
        const fixture = await installRoutes(page, persona, locale);
        await login(page, persona);
        await auditShellCatalogAndKeyboard(page, locale, persona === "account-admin" ? 2 : 1);
        await finishRecord(page, locale, width, persona, "shell-access", fixture.diagnostics, fixture.unknownRequests, {
          shell: true,
          account_principal: true,
          account_admin_controls: persona === "account-admin",
          owner_or_shared_access: persona !== "account-admin",
          permission_denied: persona === "shared-read",
          unshared_adapter_hidden: persona === "shared-edit" || persona === "shared-read",
        });
        if (persona === "account-admin") {
          await auditAdminSettings(page, locale, width, persona, fixture.unknownRequests, fixture.diagnostics);
        } else if (persona === "owner") {
          await auditOwnerSettings(page, locale, width, fixture.unknownRequests, fixture.diagnostics);
        }
        await auditTaskAndAi(page, locale, width, persona, fixture.unknownRequests, fixture.diagnostics);
        await auditAccountProfile(page, locale, width, persona, fixture.unknownRequests, fixture.diagnostics);
        await page.close();
      }
      await context.close();
    });
  }
}

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`${locale} ${width}px Wave E empty, loading, error, forced-password and disabled states`, async ({ browser }) => {
      const context = await browser.newContext({ locale, viewport: { width, height: 900 } });
      for (const persona of ["empty", "error"] as const) {
        const page = await context.newPage();
        const fixture = await installRoutes(page, persona, locale);
        await auditErrorState(page, locale, persona);
        if (persona === "empty") {
          await page.getByTestId("admin-token-input").fill("fixture-token");
          await page.getByTestId("admin-token-submit").click();
          await expect(page.locator(".ant-empty.catalog-empty")).toBeVisible();
          await expect(page.getByTestId("workbench-empty")).toBeVisible();
        }
        await finishRecord(page, locale, width, persona, "state", fixture.diagnostics, fixture.unknownRequests, {
          catalog_empty: persona === "empty",
          workbench_disabled: persona === "empty",
          api_error_feedback: persona === "error",
          disabled_primary_actions: true,
        });
        await page.close();
      }

      const forcePage = await context.newPage();
      const forceFixture = await installRoutes(forcePage, "force-password", locale);
      await forcePage.goto("/");
      await forcePage.getByTestId("account-username-input").fill("fixture-user");
      await forcePage.getByTestId("account-password-input").fill("fixture-password");
      await forcePage.getByTestId("account-login-submit").click();
      await expect(forcePage.getByTestId("account-password-submit")).toBeVisible();
      await expect(forcePage.getByTestId("account-current-password-input")).toBeVisible();
      await finishRecord(forcePage, locale, width, "force-password", "force-password", forceFixture.diagnostics, forceFixture.unknownRequests, {
        forced_password_page: true,
        password_form: true,
        logout_action: true,
      });
      await forcePage.getByTestId("account-current-password-input").fill("fixture-password");
      await forcePage.getByTestId("account-new-password-input").fill("fixture-new-password");
      await forcePage.getByTestId("account-confirm-password-input").fill("fixture-new-password");
      await forcePage.getByTestId("account-password-submit").click();
      await expect(forcePage.getByTestId("account-username-input")).toBeVisible();
      await expect(forcePage.getByTestId("account-auth-notice")).toBeVisible();
      await finishRecord(forcePage, locale, width, "force-password", "force-password-complete", forceFixture.diagnostics, forceFixture.unknownRequests, {
        forced_password_completed: true,
        return_to_login: true,
        success_notice: true,
      });
      await forcePage.close();

      const loadingPage = await context.newPage();
      const loadingUnknown: string[] = [];
      let releaseBootstrap: (() => void) | undefined;
      const bootstrapBlocked = new Promise<void>((resolvePromise) => { releaseBootstrap = resolvePromise; });
      await loadingPage.route("**/entry-mode.js", async (route) => route.fulfill({ contentType: "application/javascript", body: 'window.__DLR_ENTRY_MODE__ = "account";' }));
      await loadingPage.route("**/api/**", async (route) => {
        const request = route.request();
        const path = new URL(request.url()).pathname;
        if (path === "/api/locale" || path === "/api/auth/account/csrf") return fulfillJson(route, { locale, status: "ok" }, 200, path.endsWith("csrf") ? { "set-cookie": "dlr_account_csrf=wave-e-csrf; Path=/" } : undefined);
        if (path === "/api/auth/account/me") {
          await bootstrapBlocked;
          return fulfillJson(route, errorBody("account_session_required", "expired"), 401);
        }
        loadingUnknown.push(`${request.method()} ${path}`);
        return fulfillJson(route, errorBody("wave_e_loading_unhandled", path), 404);
      });
      const loadingDiagnostics = captureDiagnostics(loadingPage, [401]);
      await loadingPage.goto("/");
      await expect(loadingPage.locator(".account-loading .ant-skeleton")).toBeVisible();
      await finishRecord(loadingPage, locale, width, "empty", "loading", loadingDiagnostics, loadingUnknown, { loading_skeleton: true });
      releaseBootstrap?.();
      await loadingPage.close();
      await context.close();
    });
  }
}

for (const locale of LOCALES) {
  for (const width of VIEWPORTS) {
    test(`${locale} ${width}px Wave E account login error and direct unshared access stay contained`, async ({ browser }) => {
      const context = await browser.newContext({ locale, viewport: { width, height: 900 } });
      const page = await context.newPage();
      const unknownRequests: string[] = [];
      await page.route("**/entry-mode.js", async (route) => route.fulfill({ contentType: "application/javascript", body: 'window.__DLR_ENTRY_MODE__ = "account";' }));
      await page.route("**/api/**", async (route) => {
        const request = route.request();
        const path = new URL(request.url()).pathname;
        if (path === "/api/locale") return fulfillJson(route, { locale });
        if (path === "/api/auth/account/csrf") return fulfillJson(route, { status: "ok" }, 200, { "set-cookie": "dlr_account_csrf=wave-e-csrf; Path=/" });
        if (path === "/api/auth/account/me") return fulfillJson(route, errorBody("account_session_required", "Account Session is required"), 401);
        if (path === "/api/auth/account/login") return fulfillJson(route, errorBody("invalid_credentials", "Fixture credentials rejected"), 401);
        if (path === "/api/adapters/1") return fulfillJson(route, errorBody("adapter_not_found", "Adapter not found"), 404);
        unknownRequests.push(`${request.method()} ${path}`);
        return fulfillJson(route, errorBody("wave_e_login_unhandled", path), 404);
      });
      const diagnostics = captureDiagnostics(page, [401, 404]);
      await page.goto("/");
      await page.getByTestId("account-username-input").fill("fixture-user");
      await page.getByTestId("account-password-input").fill("fixture-password");
      await page.getByTestId("account-login-submit").click();
      await expect(page.getByTestId("account-login-error")).toBeVisible();
      const directUnsharedStatus = await page.evaluate(async () => (await fetch("/api/adapters/1")).status);
      expect(directUnsharedStatus).toBe(404);
      await finishRecord(page, locale, width, "shared-read", "login-error-unshared", diagnostics, unknownRequests, {
        login_error: true,
        direct_unshared_404: true,
        no_console_or_page_error: true,
      });
      await context.close();
    });
  }
}
