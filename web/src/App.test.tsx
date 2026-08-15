import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import App, {
  EDITOR_THEME_STORAGE_KEY,
  TOKEN_STORAGE_KEY,
} from "./App";
import { setAuthToken } from "./api";
import { FALLBACK_POLICY } from "./fallback-policy";
import { PRODUCTION_REFRESH_POLICY } from "./production-refresh-policy";
import { WORKER_REFRESH_POLICY } from "./worker-refresh-policy";
import type {
  Adapter,
  AiAssistResponse,
  AiCandidate,
  Execution,
  ExecutionSummary,
  VersionDetail,
  VersionSummary,
} from "./types";

// The Monaco editor is replaced by a plain textarea so tests exercise the DLR
// business integration (value / change / save) instead of the editor itself.
// readOnly is honored so the mutation-time interaction lock is testable; the
// theme prop is mirrored so theme switching stays assertable.
vi.mock("@monaco-editor/react", () => ({
  default: function Editor(props: {
    value?: string;
    onChange?: (value: string | undefined) => void;
    options?: { readOnly?: boolean };
    theme?: string;
    language?: string;
  }) {
    return (
      <textarea
        data-testid="code-editor"
        data-monaco-theme={props.theme ?? ""}
        data-monaco-language={props.language ?? ""}
        value={props.value ?? ""}
        disabled={props.options?.readOnly ?? false}
        onChange={(event) => props.onChange?.(event.target.value)}
      />
    );
  },
  // M3.2：DiffEditor 降级为两侧文本展示，便于断言 original/modified 内容。
  DiffEditor: function DiffEditor(props: {
    language?: string;
    original?: string;
    modified?: string;
  }) {
    return (
      <div
        data-testid="diff-editor"
        data-monaco-language={props.language ?? ""}
        data-original={props.original ?? ""}
        data-modified={props.modified ?? ""}
      />
    );
  },
}));

const STARTER_CODE = "def handle(context, input):\n    return input\n";

const TASK_STARTER_CODE =
  "def handle(context, input):\n" +
  "    context.logger.info(\"任务开始\")\n" +
  "    try:\n" +
  "        return {\"message\": \"hello from DLR\", \"input\": input}\n" +
  "    finally:\n" +
  "        context.logger.info(\"任务结束\")\n";

const WEBHOOK_STARTER_CODE =
  "def handle(context, input):\n" +
  "    context.logger.info(\"收到 Webhook 请求\")\n" +
  "    try:\n" +
  "        return {\"received\": True, \"data\": input}\n" +
  "    finally:\n" +
  "        context.logger.info(\"处理完 Webhook 请求\")\n";

interface RouteResponse {
  status?: number;
  body?: unknown;
  /** SSE: raw event-stream text delivered as a single chunk. */
  stream?: string;
  /** SSE: raw event-stream text delivered chunk by chunk, then EOF. */
  streamChunks?: string[];
}

interface Route {
  method: string;
  match: string | RegExp;
  respond: (body: string | null, url: string) => RouteResponse | Promise<RouteResponse>;
}

function stubFetch(routes: Route[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = (init?.method ?? "GET").toUpperCase();
    const requestBody = typeof init?.body === "string" ? init.body : null;
    const route = routes.find((candidate) => {
      if (candidate.method !== method) {
        return false;
      }
      return typeof candidate.match === "string"
        ? candidate.match === url
        : candidate.match.test(url);
    });
    if (!route) {
      throw new Error(`Unexpected request: ${method} ${url}`);
    }
    const { status = 200, body, stream, streamChunks } = await route.respond(requestBody, url);
    if (stream !== undefined || streamChunks !== undefined) {
      // Minimal ReadableStream-like body for the SSE reader; chunks arrive
      // with a small gap so intermediate renders are observable.
      const chunks = streamChunks ?? [stream ?? ""];
      let index = 0;
      const encoder = new TextEncoder();
      return {
        ok: status >= 200 && status < 300,
        status,
        body: {
          getReader: () => ({
            read: async () => {
              if (index >= chunks.length) {
                return { done: true, value: undefined };
              }
              const chunk = chunks[index];
              index += 1;
              if (index > 1) {
                await new Promise((resolve) => setTimeout(resolve, 150));
              }
              return { done: false, value: encoder.encode(chunk) };
            },
          }),
        },
      };
    }
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => {
        if (body === undefined) {
          throw new Error("no JSON body");
        }
        return body;
      },
    };
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const healthRoute = (payload: unknown, status = 200): Route => ({
  method: "GET",
  match: "/api/health",
  respond: () => ({ status, body: payload }),
});

const emptyAdaptersRoute: Route = {
  method: "GET",
  match: "/api/adapters",
  respond: () => ({ body: [] }),
};

function makeAdapter(overrides: Partial<Adapter> = {}): Adapter {
  return {
    id: 1,
    name: "adapter-a",
    description: "",
    language: "python",
    adapter_type: "task",
    run_mode: "manual",
    latest_version_id: null,
    published_version_id: null,
    created_at: "2026-08-11T00:00:00Z",
    updated_at: "2026-08-11T00:00:00Z",
    ...overrides,
  };
}

function makeVersion(overrides: Partial<VersionDetail> = {}): VersionDetail {
  return {
    id: 10,
    adapter_id: 1,
    seq: 1,
    code: STARTER_CODE,
    requirements: "",
    runtime_config: {},
    created_at: "2026-08-11T00:00:00Z",
    ...overrides,
  };
}

async function selectFirstAdapter() {
  const [first] = await screen.findAllByTestId("adapter-item");
  fireEvent.click(first);
  await screen.findByTestId("code-editor");
}

// No jest-dom in this project: read form control values directly.
function valueOf(testId: string): string {
  return (screen.getByTestId(testId) as HTMLInputElement).value;
}

// Most tests exercise the authenticated console: seed the sessionStorage token
// the App reads on mount. Auth-specific tests clear it explicitly.
beforeEach(() => {
  sessionStorage.setItem(TOKEN_STORAGE_KEY, "test-admin-token");
});

afterEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  setAuthToken(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  // Restore the production fallback pace for tests that tightened it.
  FALLBACK_POLICY.pollIntervalMs = 3000;
  FALLBACK_POLICY.maxPolls = 60;
  PRODUCTION_REFRESH_POLICY.pollIntervalMs = 3000;
  WORKER_REFRESH_POLICY.pollIntervalMs = 30_000;
});

// --- M5.4.2 Task Adapter user model -------------------------------------------

it("uses the Task starter only for Task Adapters without a saved Revision", async () => {
  const task = makeAdapter({ id: 1, name: "task-a", adapter_type: "task" });
  const webhook = makeAdapter({ id: 2, name: "webhook-a", adapter_type: "webhook" });
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [task, webhook] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/2/versions", respond: () => ({ body: [] }) },
  ]);

  render(<App />);
  const items = await screen.findAllByTestId("adapter-item");
  fireEvent.click(items[0]);
  await waitFor(() => expect(valueOf("code-editor")).toBe(TASK_STARTER_CODE));

  fireEvent.click(items[1]);
  await screen.findByRole("heading", { name: "webhook-a" });
  expect(valueOf("code-editor")).toBe(WEBHOOK_STARTER_CODE);
  expect(valueOf("code-editor")).toContain("收到 Webhook 请求");
  expect(valueOf("code-editor")).toContain("处理完 Webhook 请求");
  expect(valueOf("code-editor")).not.toContain("任务开始");
  expect(valueOf("code-editor")).not.toContain("任务结束");
});

it("shows the Task workbench without Version/Publish/Production controls", async () => {
  const task = makeAdapter({
    adapter_type: "task",
    run_mode: "manual",
    latest_version_id: 10,
    runtime_worker_id: 1,
  });
  const version = makeVersion();
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [task] }) },
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [{ id: 1, name: "task-worker", status: "online", last_heartbeat: "2026-08-15T00:00:00Z", capabilities: ["python"] }],
      }),
    },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [version] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: version }) },
  ]);

  render(<App />);
  await selectFirstAdapter();

  expect(screen.getByTestId("task-workbench-header")).toBeDefined();
  expect(screen.getByRole("tab", { name: "编辑" })).toBeDefined();
  expect(screen.getByRole("tab", { name: "运行设置" })).toBeDefined();
  expect(screen.getByRole("tab", { name: "执行记录" })).toBeDefined();
  expect(screen.queryByTestId("version-selector")).toBeNull();
  expect(screen.queryByTestId("publish-version")).toBeNull();
  expect(screen.queryByTestId("start-production")).toBeNull();
  expect(screen.queryByTestId("stop-production")).toBeNull();
  expect(document.body.textContent).not.toMatch(/Latest|Published|Production Worker|发布目标|生产入口/);
});

it("persists the Task run mode and reveals Schedule settings", async () => {
  const manual = makeAdapter({
    adapter_type: "task",
    run_mode: "manual",
    runtime_worker_id: 1,
  });
  const scheduled = { ...manual, run_mode: "schedule" as const };
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [manual] }) },
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [{ id: 1, name: "task-worker", status: "online", last_heartbeat: "2026-08-15T00:00:00Z", capabilities: ["python"] }],
      }),
    },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [] }) },
    { method: "PATCH", match: "/api/adapters/1", respond: () => ({ body: scheduled }) },
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({ status: 404, body: { detail: { code: "schedule_not_configured", message: "Schedule is not configured" } } }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  fireEvent.click(await screen.findByLabelText("定时运行"));
  fireEvent.click(screen.getByTestId("save-task-runtime"));

  await screen.findByTestId("task-schedule-cron");
  const patchCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
  );
  expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
    runtime_worker_id: 1,
    run_mode: "schedule",
  });
});

it("locks Task editing while Schedule is enabled and unlocks after disable", async () => {
  let current = makeAdapter({
    adapter_type: "task",
    run_mode: "schedule",
    latest_version_id: 10,
    runtime_worker_id: 1,
    runtime_locked: false,
  });
  let enabled = false;
  const version = makeVersion();
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [current] }) },
    { method: "GET", match: "/api/adapters/1", respond: () => ({ body: current }) },
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [{ id: 1, name: "task-worker", status: "online", last_heartbeat: "2026-08-15T00:00:00Z", capabilities: ["python"] }],
      }),
    },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [version] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: version }) },
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () =>
        enabled
          ? { body: { adapter_id: 1, enabled: true, cron: "*/5 * * * *", timezone: "Asia/Shanghai", input: {}, next_run_at: "2026-08-15T01:00:00Z", updated_at: "2026-08-15T00:00:00Z" } }
          : { status: 404, body: { detail: { code: "schedule_not_configured", message: "Schedule is not configured" } } },
    },
    {
      method: "PUT",
      match: "/api/adapters/1/schedule",
      respond: (body) => {
        enabled = JSON.parse(body ?? "{}").enabled as boolean;
        current = { ...current, runtime_locked: enabled };
        return { body: { adapter_id: 1, enabled, cron: "*/5 * * * *", timezone: "Asia/Shanghai", input: {}, next_run_at: enabled ? "2026-08-15T01:00:00Z" : null, updated_at: "2026-08-15T00:00:00Z" } };
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  fireEvent.click(await screen.findByTestId("enable-task-schedule"));
  await screen.findByTestId("disable-task-schedule");
  fireEvent.click(screen.getByRole("tab", { name: "编辑" }));
  await waitFor(() => expect((screen.getByTestId("code-editor") as HTMLTextAreaElement).disabled).toBe(true));

  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  fireEvent.click(screen.getByTestId("disable-task-schedule"));
  await screen.findByTestId("enable-task-schedule");
  fireEvent.click(screen.getByRole("tab", { name: "编辑" }));
  await waitFor(() => expect((screen.getByTestId("code-editor") as HTMLTextAreaElement).disabled).toBe(false));
});

it.each([
  {
    name: "a saved Revision",
    adapter: makeAdapter({
      adapter_type: "task",
      run_mode: "schedule",
      latest_version_id: null,
      runtime_worker_id: 1,
    }),
    expectedReason: "请先保存 Revision，再启用定时。",
  },
  {
    name: "a configured runtime Worker",
    adapter: makeAdapter({
      adapter_type: "task",
      run_mode: "schedule",
      latest_version_id: 10,
      runtime_worker_id: null,
    }),
    expectedReason: "请先保存运行节点，再启用定时。",
  },
])("blocks Schedule enablement without $name", async ({ adapter, expectedReason }) => {
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: adapter.latest_version_id === null ? [] : [makeVersion()] }),
    },
    ...(adapter.latest_version_id === null
      ? []
      : [{
          method: "GET",
          match: "/api/adapters/1/versions/10",
          respond: () => ({ body: makeVersion() }),
        }]),
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({
        status: 404,
        body: {
          detail: { code: "schedule_not_configured", message: "Schedule is not configured" },
        },
      }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));

  const enable = await screen.findByTestId("enable-task-schedule") as HTMLButtonElement;
  expect(enable.disabled).toBe(true);
  expect(screen.getByText(expectedReason)).toBeDefined();
  fireEvent.click(enable);
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/schedule" && init?.method === "PUT",
    ),
  ).toBe(false);
});

// --- Admin token auth (M2) -----------------------------------------------------

it("shows the admin token input when no token is stored", async () => {
  sessionStorage.clear();
  // No routes registered: the login screen must not call any API.
  stubFetch([]);
  render(<App />);
  await screen.findByTestId("admin-token-input");
  expect(screen.getByTestId("admin-token-submit")).toBeDefined();
});

it("enters the console after a valid token is verified", async () => {
  sessionStorage.clear();
  stubFetch([
    {
      method: "GET",
      match: "/api/auth/admin/verify",
      respond: () => ({ body: { status: "ok" } }),
    },
    healthRoute({ status: "ok", database: true }),
    emptyAdaptersRoute,
  ]);
  render(<App />);
  fireEvent.change(screen.getByTestId("admin-token-input"), {
    target: { value: "correct-token" },
  });
  fireEvent.click(screen.getByTestId("admin-token-submit"));
  await screen.findByTestId("control-status");
  expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe("correct-token");
});

it("rejects a wrong token and stays on the login screen", async () => {
  sessionStorage.clear();
  stubFetch([
    {
      method: "GET",
      match: "/api/auth/admin/verify",
      respond: () => ({
        status: 401,
        body: { detail: { code: "unauthorized", message: "Invalid credentials" } },
      }),
    },
  ]);
  render(<App />);
  fireEvent.change(screen.getByTestId("admin-token-input"), { target: { value: "wrong" } });
  fireEvent.click(screen.getByTestId("admin-token-submit"));
  await screen.findByTestId("login-error");
  expect(screen.getByTestId("admin-token-input")).toBeDefined();
  expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
});

it("sends the stored token as a Bearer header on API requests", async () => {
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    emptyAdaptersRoute,
  ]);
  render(<App />);
  await waitFor(() => {
    const call = fetchMock.mock.calls.find(([input]) => input === "/api/adapters");
    expect(call).toBeDefined();
  });
  const call = fetchMock.mock.calls.find(([input]) => input === "/api/adapters");
  const headers = call?.[1]?.headers as Record<string, string>;
  expect(headers.Authorization).toBe("Bearer test-admin-token");
});

it("clears the session token and returns to login after a 401", async () => {
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({
        status: 401,
        body: { detail: { code: "unauthorized", message: "Invalid credentials" } },
      }),
    },
  ]);
  render(<App />);
  await screen.findByTestId("admin-token-input");
  expect(screen.getByTestId("auth-notice")).toBeDefined();
  expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
});

// --- Control health indicator (kept from M0) --------------------------------

it("shows ok when control health is ok", async () => {
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    emptyAdaptersRoute,
  ]);
  render(<App />);
  await waitFor(() => {
    expect(screen.getByTestId("control-status").textContent).toBe("Control 健康");
  });
});

it("shows degraded when control returns 503 with a valid health payload", async () => {
  stubFetch([
    healthRoute({ status: "degraded", database: false }, 503),
    emptyAdaptersRoute,
  ]);
  render(<App />);
  await waitFor(() => {
    expect(screen.getByTestId("control-status").textContent).toBe("Control 降级");
  });
});

it("shows unreachable when the health request fails", async () => {
  stubFetch([
    {
      method: "GET",
      match: "/api/health",
      respond: () => {
        throw new Error("network down");
      },
    },
    emptyAdaptersRoute,
  ]);
  render(<App />);
  await waitFor(() => {
    expect(screen.getByTestId("control-status").textContent).toBe("Control 不可达");
  });
});

it("does not show ok for the contradictory payload {status: ok, database: false}", async () => {
  stubFetch([healthRoute({ status: "ok", database: false }), emptyAdaptersRoute]);
  render(<App />);
  await waitFor(() => {
    expect(screen.getByTestId("control-status").textContent).toBe("Control 不可达");
  });
});

// --- Adapter list / create ---------------------------------------------------

it("loads the adapter list", async () => {
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [makeAdapter(), makeAdapter({ id: 2, name: "adapter-b" })] }),
    },
  ]);
  render(<App />);
  await waitFor(() => {
    // M3.1: catalog rows are dense two-line items; the name line stays assertable.
    expect(
      screen
        .getAllByTestId("adapter-item")
        .map((item) => item.querySelector(".catalog-item-name")?.textContent ?? ""),
    ).toEqual(["adapter-a", "adapter-b"]);
  });
});

it("creates an adapter and selects it", async () => {
  const adapters: Adapter[] = [];
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: adapters }),
    },
    {
      method: "POST",
      match: "/api/adapters",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}") as {
          name: string;
          description: string;
          language: Adapter["language"];
        };
        const created = makeAdapter({
          name: payload.name,
          description: payload.description,
          language: payload.language,
        });
        adapters.push(created);
        return { status: 201, body: created };
      },
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [] }),
    },
  ]);
  render(<App />);
  await screen.findByText("暂无 Adapter");

  fireEvent.click(screen.getByTestId("show-create-form"));
  await screen.findByTestId("new-adapter-name");
  expect(screen.getByRole("textbox", { name: "Adapter 名称" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Adapter 描述" })).toBeTruthy();
  expect(screen.getByRole("radiogroup", { name: "Adapter 开发语言" })).toBeTruthy();
  fireEvent.change(screen.getByTestId("new-adapter-name"), { target: { value: "cmdb-sync" } });
  fireEvent.change(screen.getByTestId("new-adapter-description"), {
    target: { value: "sync cmdb" },
  });
  fireEvent.click(screen.getByTestId("create-adapter"));

  // Created adapter becomes selected; metadata moved to the settings drawer.
  await screen.findByRole("heading", { name: "cmdb-sync" });
  fireEvent.click(screen.getByTestId("adapter-settings"));
  await screen.findByTestId("adapter-name");
  expect(valueOf("adapter-name")).toBe("cmdb-sync");

  const createCall = fetchMock.mock.calls.find(
    ([url, init]) => url === "/api/adapters" && init?.method === "POST",
  );
  expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
    name: "cmdb-sync",
    description: "sync cmdb",
    language: "python",
    adapter_type: "task",
  });
});

it("creates a JavaScript adapter with the language starter and Monaco mode", async () => {
  const adapters: Adapter[] = [];
  let createBody = "";
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: adapters }),
    },
    {
      method: "POST",
      match: "/api/adapters",
      respond: (body) => {
        createBody = body ?? "";
        const created = makeAdapter({
          name: "node-adapter",
          language: "javascript",
          adapter_type: "webhook",
        });
        adapters.push(created);
        return { status: 201, body: created };
      },
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [] }),
    },
  ]);
  render(<App />);
  await screen.findByText("暂无 Adapter");
  fireEvent.click(screen.getByTestId("show-create-form"));
  fireEvent.change(screen.getByTestId("new-adapter-name"), {
    target: { value: "node-adapter" },
  });
  fireEvent.click(screen.getByText("JavaScript"));
  fireEvent.click(screen.getByRole("radio", { name: "Webhook Adapter" }));
  fireEvent.click(screen.getByTestId("create-adapter"));

  const editor = await screen.findByTestId("code-editor");
  expect(JSON.parse(createBody).language).toBe("javascript");
  expect(JSON.parse(createBody).adapter_type).toBe("webhook");
  expect((editor as HTMLTextAreaElement).value).toContain("export async function handle");
  expect(editor.getAttribute("data-monaco-language")).toBe("javascript");
  expect(screen.getByText("npm 依赖")).toBeTruthy();

  fireEvent.click(screen.getByTestId("show-create-form"));
  expect((screen.getByRole("radio", { name: "Python" }) as HTMLInputElement).checked).toBe(true);
});

// --- Version editing ---------------------------------------------------------

it("shows the browser-only starter code when the adapter has no versions", async () => {
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [makeAdapter()] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [] }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();

  expect(valueOf("code-editor")).toBe(TASK_STARTER_CODE);
  // The starter must not be persisted before an explicit Save.
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url).includes("/versions") && init?.method === "POST",
    ),
  ).toBe(false);
});

it("blocks saving when runtime config is not a JSON object", async () => {
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [makeAdapter()] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [] }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();

  // M3.2：运行参数位于次级配置 Tabs，需先激活对应页签。
  fireEvent.click(screen.getByText("运行参数（JSON）"));
  fireEvent.change(screen.getByTestId("runtime-config-input"), { target: { value: "[1, 2" } });
  fireEvent.click(screen.getByTestId("save-version"));

  await screen.findByTestId("error-banner");
  expect(screen.getByTestId("error-banner").textContent).toContain("Runtime config");
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url).endsWith("/versions") && init?.method === "POST",
    ),
  ).toBe(false);
});

it("saves a new version with the edited content", async () => {
  const versions: VersionSummary[] = [];
  let adapter = makeAdapter();
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [adapter] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: versions }),
    },
    {
      method: "POST",
      match: "/api/adapters/1/versions",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}") as {
          code: string;
          requirements: string;
          runtime_config: Record<string, unknown>;
        };
        const saved = makeVersion({
          code: payload.code,
          requirements: payload.requirements,
          runtime_config: payload.runtime_config,
        });
        versions.push(saved);
        adapter = { ...adapter, latest_version_id: saved.id };
        return { status: 201, body: saved };
      },
    },
    {
      method: "GET",
      match: "/api/adapters/1",
      respond: () => ({ body: adapter }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();

  fireEvent.change(screen.getByTestId("code-editor"), {
    target: { value: "def handle(context, input):\n    return {'done': True}\n" },
  });
  fireEvent.change(screen.getByTestId("requirements-input"), {
    target: { value: "requests==2.32.0" },
  });
  // M3.2：运行参数位于次级配置 Tabs，需先激活对应页签。
  fireEvent.click(screen.getByText("运行参数（JSON）"));
  fireEvent.change(screen.getByTestId("runtime-config-input"), {
    target: { value: '{"batch": 10}' },
  });
  fireEvent.click(screen.getByTestId("save-version"));

  await waitFor(() => expect(screen.getByTestId("task-revision").textContent).toContain("Revision 1"));
  const saveCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url).endsWith("/versions") && init?.method === "POST",
  );
  expect(JSON.parse(String(saveCall?.[1]?.body))).toEqual({
    code: "def handle(context, input):\n    return {'done': True}\n",
    requirements: "requests==2.32.0",
    runtime_config: { batch: 10 },
  });
  // The header version selector now shows the acknowledged version.
  expect(screen.getByTestId("task-revision").textContent).toContain("Revision 1");
  expect(await screen.findByText("已保存为 v1")).toBeTruthy();
});

it("asks for confirmation before discarding unsaved changes on adapter switch", async () => {
  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [makeAdapter(), makeAdapter({ id: 2, name: "adapter-b" })] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [] }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();

  fireEvent.change(screen.getByTestId("code-editor"), { target: { value: "edited" } });
  await screen.findByTestId("dirty-indicator");

  fireEvent.click(screen.getAllByTestId("adapter-item")[1]);
  expect(confirmSpy).toHaveBeenCalled();
  // Confirmation denied: still on adapter-a with the edited content.
  expect(screen.getByRole("heading", { name: "adapter-a" })).toBeTruthy();
  expect(valueOf("code-editor")).toBe("edited");
});

it("shows failed API responses as errors instead of pretending success", async () => {
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({
        status: 500,
        body: { detail: { code: "boom", message: "server exploded" } },
      }),
    },
  ]);
  render(<App />);
  await screen.findByTestId("error-banner");
  expect(screen.getByTestId("error-banner").textContent).toContain("server exploded");
  expect(screen.getByTestId("error-banner").textContent).toContain("boom");
  expect(screen.queryAllByTestId("adapter-item")).toHaveLength(0);
});

it("shows the domain error code when creating a duplicate adapter", async () => {
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [makeAdapter()] }),
    },
    {
      method: "POST",
      match: "/api/adapters",
      respond: () => ({
        status: 409,
        body: { detail: { code: "adapter_name_conflict", message: "Adapter name already exists" } },
      }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [] }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();

  fireEvent.click(screen.getByTestId("show-create-form"));
  await screen.findByTestId("new-adapter-name");
  fireEvent.change(screen.getByTestId("new-adapter-name"), { target: { value: "adapter-a" } });
  fireEvent.click(screen.getByTestId("create-adapter"));

  await screen.findByTestId("error-banner");
  expect(screen.getByTestId("error-banner").textContent).toContain("adapter_name_conflict");
});

// --- Review regressions: atomic content loading ------------------------------

it("keeps Save disabled and never mixes content when switching to an adapter whose load fails", async () => {
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [makeAdapter(), makeAdapter({ id: 2, name: "adapter-b" })] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [] }),
    },
    {
      method: "GET",
      match: "/api/adapters/2/versions",
      respond: () => ({
        status: 500,
        body: { detail: { code: "boom", message: "version list exploded" } },
      }),
    },
    {
      // Any save attempt against a failed load must never reach the API.
      method: "POST",
      match: /\/versions$/,
      respond: () => {
        throw new Error("save must not be issued against content that failed to load");
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  expect(valueOf("code-editor")).toBe(TASK_STARTER_CODE);

  fireEvent.click(screen.getAllByTestId("adapter-item")[1]);
  await screen.findByTestId("error-banner");

  // adapter-b is selected, but adapter-a's content must not leak into it and
  // Save must stay disabled because nothing loaded successfully for adapter-b.
  expect(screen.getByRole("heading", { name: "adapter-b" })).toBeTruthy();
  expect(valueOf("code-editor")).not.toBe(TASK_STARTER_CODE);
  const saveButton = screen.getByTestId("save-version") as HTMLButtonElement;
  expect(saveButton.disabled).toBe(true);
  expect(saveButton.closest(".action-with-reason")?.getAttribute("title")).toContain(
    "版本内容尚未就绪",
  );
  fireEvent.click(screen.getByTestId("save-version"));
});

it("ignores out-of-order content loads when switching adapters rapidly", async () => {
  // adapter-a's version list is deliberately held pending; adapter-b loads
  // immediately. When the stale adapter-a response finally resolves, it must
  // be discarded instead of overwriting adapter-b's content.
  let resolveStale: ((value: VersionSummary[]) => void) | undefined;
  const staleVersions = new Promise<VersionSummary[]>((resolve) => {
    resolveStale = resolve;
  });
  let staleDelivered = false;
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({
        body: [
          makeAdapter({ latest_version_id: 10 }),
          makeAdapter({ id: 2, name: "adapter-b" }),
        ],
      }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: async () => {
        const list = await staleVersions;
        staleDelivered = true;
        return { body: list };
      },
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions/10",
      respond: () => ({ body: makeVersion({ id: 10, code: "code-a" }) }),
    },
    {
      method: "GET",
      match: "/api/adapters/2/versions",
      respond: () => ({ body: [] }),
    },
  ]);
  render(<App />);
  const items = await screen.findAllByTestId("adapter-item");

  // Rapidly switch a -> b while adapter-a's load is still in flight.
  fireEvent.click(items[0]);
  fireEvent.click(items[1]);
  await waitFor(() => {
    expect((screen.getByTestId("save-version") as HTMLButtonElement).disabled).toBe(false);
  });
  expect(screen.getByRole("heading", { name: "adapter-b" })).toBeTruthy();
  expect(valueOf("code-editor")).toBe(TASK_STARTER_CODE);

  // Now the stale adapter-a response resolves; it must not overwrite b's state.
  resolveStale?.([{ id: 10, adapter_id: 1, seq: 1, created_at: "2026-08-11T00:00:00Z" }]);
  await waitFor(() => {
    expect(staleDelivered).toBe(true);
  });
  // Give any (broken) stale continuation a chance to commit before asserting.
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(screen.getByRole("heading", { name: "adapter-b" })).toBeTruthy();
  expect(valueOf("code-editor")).toBe(TASK_STARTER_CODE);
  expect((screen.getByTestId("save-version") as HTMLButtonElement).disabled).toBe(false);
  // The stale load never progressed to fetching adapter-a's version detail.
  expect(
    fetchMock.mock.calls.some(([url]) => String(url) === "/api/adapters/1/versions/10"),
  ).toBe(false);
});

// --- Review regressions: Save acknowledgement --------------------------------

it("acknowledges a successful Save locally even when the follow-up refresh fails", async () => {
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [makeAdapter()] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: (() => {
        let calls = 0;
        return () => {
          calls += 1;
          if (calls === 1) {
            // First call: initial load of the adapter (no versions yet).
            return { body: [] };
          }
          // The refresh after a successful Save fails.
          throw new Error("refresh failed");
        };
      })(),
    },
    {
      method: "POST",
      match: "/api/adapters/1/versions",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}") as { code: string };
        return { status: 201, body: makeVersion({ code: payload.code }) };
      },
    },
    {
      // Best-effort Adapter refresh after Save succeeds.
      method: "GET",
      match: "/api/adapters/1",
      respond: () => ({ body: makeAdapter({ latest_version_id: 10 }) }),
    },
  ]);
  render(<App />);

  const [first] = await screen.findAllByTestId("adapter-item");
  fireEvent.click(first);
  // Wait until the starter snapshot actually loaded (Save becomes enabled).
  await waitFor(() => {
    expect((screen.getByTestId("save-version") as HTMLButtonElement).disabled).toBe(false);
  });

  fireEvent.change(screen.getByTestId("code-editor"), { target: { value: "saved code" } });
  fireEvent.click(screen.getByTestId("save-version"));

  // The refresh failure is reported as a refresh problem, not a failed save.
  await screen.findByTestId("error-banner");
  expect(screen.getByTestId("error-banner").textContent).toContain("版本已保存");

  // The saved version is acknowledged: not dirty, selected, and marked latest,
  // so the user is never encouraged to repeat an already-successful save.
  expect(screen.queryByTestId("dirty-indicator")).toBeNull();
  expect(screen.getByTestId("task-revision").textContent).toContain("Revision 1");
  expect(valueOf("code-editor")).toBe("saved code");
});

// --- Review regressions: create form only closes on real success -------------

it("keeps the create form and its inputs when creation fails with a duplicate name", async () => {
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [makeAdapter()] }),
    },
    {
      method: "POST",
      match: "/api/adapters",
      respond: () => ({
        status: 409,
        body: { detail: { code: "adapter_name_conflict", message: "Adapter name already exists" } },
      }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [] }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();

  fireEvent.click(screen.getByTestId("show-create-form"));
  await screen.findByTestId("new-adapter-name");
  fireEvent.change(screen.getByTestId("new-adapter-name"), { target: { value: "adapter-a" } });
  fireEvent.change(screen.getByTestId("new-adapter-description"), {
    target: { value: "keep me" },
  });
  fireEvent.click(screen.getByTestId("create-adapter"));

  await screen.findByTestId("error-banner");
  // The form stays open with the user's input still editable.
  expect(screen.getByTestId("new-adapter-name")).toBeTruthy();
  expect(valueOf("new-adapter-name")).toBe("adapter-a");
  expect(valueOf("new-adapter-description")).toBe("keep me");
});

it("keeps the create form open when creation is cancelled by the discard confirmation", async () => {
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [makeAdapter()] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [] }),
    },
    {
      // Creation must never be attempted when the discard confirmation is denied.
      method: "POST",
      match: "/api/adapters",
      respond: () => {
        throw new Error("create must not be issued after a denied discard confirmation");
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.change(screen.getByTestId("code-editor"), { target: { value: "edited" } });
  await screen.findByTestId("dirty-indicator");

  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.click(screen.getByTestId("show-create-form"));
  await screen.findByTestId("new-adapter-name");
  fireEvent.change(screen.getByTestId("new-adapter-name"), { target: { value: "new-one" } });
  fireEvent.click(screen.getByTestId("create-adapter"));

  expect(confirmSpy).toHaveBeenCalled();
  // Form stays open with the typed name; nothing was created.
  expect(valueOf("new-adapter-name")).toBe("new-one");
  expect(screen.getByRole("heading", { name: "adapter-a" })).toBeTruthy();
  expect(valueOf("code-editor")).toBe("edited");
});

// --- Review round 2 regressions: interaction lock during mutations ----------

it("locks editing while Save is in flight so the saved snapshot stays consistent", async () => {
  let resolveSave: ((value: VersionDetail) => void) | undefined;
  const saveResponse = new Promise<VersionDetail>((resolve) => {
    resolveSave = resolve;
  });
  const versions: VersionSummary[] = [];
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [makeAdapter()] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: versions }),
    },
    {
      method: "POST",
      match: "/api/adapters/1/versions",
      respond: async (body) => {
        const payload = JSON.parse(body ?? "{}") as {
          code: string;
          requirements: string;
          runtime_config: Record<string, unknown>;
        };
        const saved = makeVersion({ ...payload });
        return { status: 201, body: await saveResponse.then(() => saved) };
      },
    },
    {
      method: "GET",
      match: "/api/adapters/1",
      respond: () => ({ body: makeAdapter({ latest_version_id: 10 }) }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  // M3.2：激活运行参数页签使其进入 DOM，随后验证 Save 期间的编辑锁。
  fireEvent.click(screen.getByText("运行参数（JSON）"));

  fireEvent.change(screen.getByTestId("code-editor"), { target: { value: "edited code" } });
  fireEvent.click(screen.getByTestId("save-version"));

  // While Save is pending, every editing/navigation surface is locked.
  await waitFor(() => {
    expect((screen.getByTestId("code-editor") as HTMLTextAreaElement).disabled).toBe(true);
  });
  expect((screen.getByTestId("requirements-input") as HTMLTextAreaElement).disabled).toBe(true);
  expect((screen.getByTestId("runtime-config-input") as HTMLTextAreaElement).disabled).toBe(true);
  expect((screen.getByTestId("save-version") as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getAllByTestId("adapter-item")[0] as HTMLButtonElement).disabled).toBe(true);

  // Save completes with exactly the snapshot that existed when it started.
  const savedVersion = makeVersion({ code: "edited code", requirements: "", runtime_config: {} });
  versions.push(savedVersion);
  resolveSave?.(savedVersion);
  await waitFor(() => expect(screen.getByTestId("task-revision").textContent).toContain("Revision 1"));

  const saveCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url).endsWith("/versions") && init?.method === "POST",
  );
  const sentPayload = JSON.parse(String(saveCall?.[1]?.body)) as { code: string };
  expect(sentPayload.code).toBe("edited code");
  expect(valueOf("code-editor")).toBe("edited code");
  expect(screen.queryByTestId("dirty-indicator")).toBeNull();
});

it("never fabricates Adapter.updated_at from the saved version; adapter refresh failure is non-fatal", async () => {
  const adapterBefore = makeAdapter();
  let savedVersion: VersionDetail | null = null;
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [adapterBefore] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({
        body: savedVersion === null
          ? []
          : [{
              id: savedVersion.id,
              adapter_id: savedVersion.adapter_id,
              seq: savedVersion.seq,
              created_at: savedVersion.created_at,
            }],
      }),
    },
    {
      method: "POST",
      match: "/api/adapters/1/versions",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}") as { code: string };
        savedVersion = makeVersion({ code: payload.code });
        return { status: 201, body: savedVersion };
      },
    },
    {
      method: "GET",
      match: "/api/adapters/1",
      respond: () => {
        throw new Error("adapter refresh failed");
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();

  fireEvent.change(screen.getByTestId("code-editor"), { target: { value: "saved code" } });
  fireEvent.click(screen.getByTestId("save-version"));

  // The save is still acknowledged (latest badge, not dirty) while the failed
  // Adapter refresh is reported separately; the server-owned updated_at is
  // never synthesized from the version's created_at.
  await waitFor(() => expect(screen.getByTestId("task-revision").textContent).toContain("Revision 1"));
  expect(screen.getByTestId("error-banner").textContent).toContain("版本已保存");
  expect(screen.getByTestId("error-banner").textContent).toContain("刷新 Adapter 失败");
  expect(screen.queryByTestId("dirty-indicator")).toBeNull();
  expect(valueOf("code-editor")).toBe("saved code");
  expect(
    fetchMock.mock.calls.some(([url]) => String(url) === "/api/adapters/1"),
  ).toBe(true);
});

// --- Test run and execution observability (M3) --------------------------------

function makeExecution(overrides: Partial<Execution> = {}): Execution {
  return {
    id: 5,
    adapter_id: 1,
    version_id: 10,
    worker_id: null,
    target_worker_id: null,
    trigger: "manual",
    scheduled_for: null,
    status: "pending",
    input: {},
    output: null,
    output_size: null,
    output_truncated: false,
    output_preview: null,
    stdout: "",
    stdout_truncated: false,
    stderr: "",
    stderr_truncated: false,
    error: null,
    created_at: "2026-08-11T00:00:00Z",
    started_at: null,
    ended_at: null,
    duration_ms: null,
    ...overrides,
  };
}

function makeSummary(overrides: Partial<ExecutionSummary> = {}): ExecutionSummary {
  return {
    id: 6,
    adapter_id: 1,
    version_id: 10,
    version_seq: 1,
    worker_id: null,
    worker_name: null,
    trigger: "manual",
    scheduled_for: null,
    status: "succeeded",
    created_at: "2026-08-11T00:00:00Z",
    started_at: null,
    ended_at: null,
    duration_ms: null,
    ...overrides,
  };
}

// Routes shared by M3 tests: healthy control plus one adapter whose latest
// version (id 10) is already loaded into the editor.
function consoleWithVersionRoutes(adapter: Adapter, version: VersionDetail): Route[] {
  return [
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    {
      method: "GET",
      match: `/api/adapters/${adapter.id}/versions`,
      respond: () => ({
        body: [
          {
            id: version.id,
            adapter_id: version.adapter_id,
            seq: version.seq,
            created_at: version.created_at,
          } satisfies VersionSummary,
        ],
      }),
    },
    {
      method: "GET",
      match: `/api/adapters/${adapter.id}/versions/${version.id}`,
      respond: () => ({ body: version }),
    },
  ];
}

it("lists unfiltered Task execution history with cursor pagination and opens detail", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const pageOne = {
    items: [makeSummary({ id: 6, version_seq: 7, worker_id: 3, worker_name: "worker-main" })],
    next_before_id: 6,
  };
  const pageTwo = {
    items: [makeSummary({ id: 4, version_seq: 6, worker_id: 4, worker_name: "worker-alt", status: "failed" })],
    next_before_id: null,
  };
  const detail = makeExecution({ id: 6, worker_id: 3, status: "succeeded", input: { k: 1 }, output: { ok: true } });
  const secondDetail = makeExecution({ id: 4, worker_id: 4, status: "failed", input: { k: 2 } });
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: /\/api\/adapters\/1\/executions\?/,
      respond: (_body, url) => ({
        body: url.includes("before_id=6") ? pageTwo : pageOne,
      }),
    },
    { method: "GET", match: "/api/executions/6", respond: () => ({ body: detail }) },
    { method: "GET", match: "/api/executions/4", respond: () => ({ body: secondDetail }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  // History is only requested when the tab is activated.
  fireEvent.click(screen.getByText("执行记录"));
  expect(await screen.findAllByTestId("history-row")).toHaveLength(1);
  const firstHistoryUrl = String(
    fetchMock.mock.calls.find(([url]) => String(url).startsWith("/api/adapters/1/executions?"))?.[0],
  );
  expect(firstHistoryUrl).not.toContain("trigger=");

  fireEvent.click(screen.getByTestId("history-load-more"));
  await waitFor(() => {
    expect(screen.getAllByTestId("history-row")).toHaveLength(2);
  });
  expect(screen.queryByTestId("history-load-more")).toBeNull();

  const [firstRow, secondRow] = screen.getAllByTestId("history-row");
  expect(firstRow.getAttribute("tabindex")).toBe("0");
  firstRow.focus();
  fireEvent.keyDown(firstRow, { key: "Enter" });
  const detailInput = await screen.findByTestId("detail-input");
  expect(detailInput.textContent).toContain('"k": 1');
  const drawer = document.querySelector(".ant-drawer-content");
  if (!(drawer instanceof HTMLElement)) {
    throw new Error("Execution detail drawer not found");
  }
  expect(within(drawer).getByText("v7")).toBeTruthy();
  expect(within(drawer).getByText("worker-main")).toBeTruthy();
  expect(within(drawer).getByText("#10")).toBeTruthy();
  expect(within(drawer).getByText("#3")).toBeTruthy();

  fireEvent.click(drawer.querySelector(".ant-drawer-close") as HTMLButtonElement);
  secondRow.focus();
  expect(fireEvent.keyDown(secondRow, { key: " " })).toBe(false);
  await screen.findByText("Execution #4");
  expect((await screen.findByTestId("detail-input")).textContent).toContain('"k": 2');
});

it("never shows a stale detail when executions are clicked in quick succession", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const page = {
    items: [makeSummary({ id: 6 }), makeSummary({ id: 4, status: "failed" })],
    next_before_id: null,
  };
  const detailA = makeExecution({ id: 6, input: { who: "A" } });
  const detailB = makeExecution({ id: 4, input: { who: "B" }, status: "failed" });
  let releaseA: () => void = () => {};
  const gateA = new Promise<void>((resolve) => {
    releaseA = resolve;
  });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: /\/api\/adapters\/1\/executions\?/,
      respond: () => ({ body: page }),
    },
    {
      method: "GET",
      match: "/api/executions/6",
      // A is slow: its response only resolves after B is already shown.
      respond: async () => {
        await gateA;
        return { body: detailA };
      },
    },
    { method: "GET", match: "/api/executions/4", respond: () => ({ body: detailB }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByText("执行记录"));
  const rows = await screen.findAllByTestId("history-row");
  expect(rows).toHaveLength(2);

  // Once B is visible, opening slow A must hide B immediately instead of
  // presenting stale details under the new Execution title.
  fireEvent.click(rows[1]);
  await screen.findByText("Execution #4");
  expect(screen.getByTestId("detail-input").textContent).toContain('"who": "B"');
  fireEvent.click(rows[0]);
  expect(screen.queryByTestId("detail-input")).toBeNull();

  // Click B again while A is still slow: B must win even though A resolves last.
  fireEvent.click(rows[1]);
  await screen.findByText("Execution #4");
  expect(screen.getByTestId("detail-input").textContent).toContain('"who": "B"');

  releaseA();
  // Let A's late response settle, then verify the drawer still shows B.
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(screen.getByTestId("detail-input").textContent).toContain('"who": "B"');
  expect(screen.getByText("Execution #4")).toBeTruthy();
});

it("shows the worker badge by online presence, not by registration count", async () => {
  const offlineWorker = {
    id: 1,
    name: "worker-a",
    status: "offline",
    last_heartbeat: "2026-08-11T00:00:00Z",
    capabilities: ["python"],
  };
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    emptyAdaptersRoute,
    { method: "GET", match: "/api/workers", respond: () => ({ body: [offlineWorker] }) },
  ]);
  const { unmount } = render(<App />);
  fireEvent.click(await screen.findByTestId("worker-status"));
  const [offlineItem] = await screen.findAllByTestId("worker-item");
  expect(within(offlineItem).getByText("offline")).toBeTruthy();
  expect(
    screen.getByText("在线状态已结合最近心跳和超时阈值判定；最近心跳时间用于排障。"),
  ).toBeTruthy();
  expect(screen.queryByText(/平台不做心跳超时判定/)).toBeNull();
  expect(
    screen.getByTestId("worker-status").querySelector(".ant-badge-status-error"),
  ).toBeTruthy();
  expect(
    screen.getByTestId("worker-status").querySelector(".ant-badge-status-success"),
  ).toBeNull();
  expect(screen.getByTestId("worker-status").textContent).toContain("0/1 在线");
  unmount();

  stubFetch([
    healthRoute({ status: "ok", database: true }),
    emptyAdaptersRoute,
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [offlineWorker, { ...offlineWorker, id: 2, name: "worker-b", status: "online" }],
      }),
    },
  ]);
  render(<App />);
  fireEvent.click(await screen.findByTestId("worker-status"));
  await screen.findAllByTestId("worker-item");
  expect(screen.getByTestId("worker-status").textContent).toContain("1/2 在线");
  expect(
    screen.getByTestId("worker-status").querySelector(".ant-badge-status-success"),
  ).toBeTruthy();
});

it("refreshes the shared effective Worker status on focus without a page reload", async () => {
  const adapter = makeAdapter({
    latest_version_id: 10,
    published_version_id: 10,
    runtime_worker_id: 1,
  });
  const worker = {
    id: 1,
    name: "worker-a",
    last_heartbeat: "2026-08-13T00:00:00Z",
    capabilities: ["python"],
  };
  let workerCalls = 0;
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: "/api/workers",
      respond: () => {
        workerCalls += 1;
        return {
          body: [{ ...worker, status: workerCalls === 2 ? "offline" : "online" }],
        };
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await waitFor(() => {
    expect(screen.getByTestId("worker-status").textContent).toContain("1/1 在线");
  });
  window.dispatchEvent(new Event("focus"));
  await waitFor(() => {
    expect(workerCalls).toBe(2);
    expect(screen.getByTestId("worker-status").textContent).toContain("0/1 在线");
  });
  expect(
    screen.getByTestId("adapter-item").querySelector(".catalog-item-attention")?.textContent,
  ).toContain("Worker 离线");

  window.dispatchEvent(new Event("focus"));
  await waitFor(() => {
    expect(workerCalls).toBe(3);
    expect(screen.getByTestId("worker-status").textContent).toContain("1/1 在线");
  });
  expect(
    screen.getByTestId("adapter-item").querySelector(".catalog-item-attention"),
  ).toBeNull();
});

it("refreshes the shared Worker collection in the background without returning to loading", async () => {
  WORKER_REFRESH_POLICY.pollIntervalMs = 50;
  const offlineWorker = {
    id: 1,
    name: "worker-a",
    status: "offline",
    last_heartbeat: "2026-08-13T00:00:00Z",
    capabilities: ["python"],
  };
  let workerCalls = 0;
  let releaseOnline!: () => void;
  const onlineResponse = new Promise<RouteResponse>((resolve) => {
    releaseOnline = () => resolve({ body: [{ ...offlineWorker, status: "online" }] });
  });
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    emptyAdaptersRoute,
    {
      method: "GET",
      match: "/api/workers",
      respond: () => {
        workerCalls += 1;
        return workerCalls === 1 ? { body: [offlineWorker] } : onlineResponse;
      },
    },
  ]);

  render(<App />);
  await waitFor(() => {
    expect(screen.getByTestId("worker-status").textContent).toContain("0/1 在线");
  });
  await waitFor(() => {
    expect(workerCalls).toBe(2);
  });
  expect(screen.getByTestId("worker-status").textContent).toContain("0/1 在线");
  expect(screen.getByTestId("worker-status").textContent).not.toContain("加载中");

  releaseOnline();
  await waitFor(() => {
    expect(screen.getByTestId("worker-status").textContent).toContain("1/1 在线");
  });
});

// --- M3.1 Console shell structure ---------------------------------------------

it("shows the brand area and the token card on the login page", async () => {
  sessionStorage.clear();
  // No routes registered: the login screen must not call any API.
  stubFetch([]);
  render(<App />);
  await screen.findByTestId("admin-token-input");
  expect(screen.getByText("DataLinkRuntime")).toBeTruthy();
  expect(screen.getByText("欢迎登录 DLR 控制台")).toBeTruthy();
  expect(screen.getByText("轻量数据适配运行平台")).toBeTruthy();
  // 长期产品定位直接展示（Issue #8 产品视觉决策补充），“AI 辅助”仅为定位文案。
  expect(screen.getByText("轻量易用")).toBeTruthy();
  expect(screen.getByText("核心精简，快速部署")).toBeTruthy();
  expect(screen.getByText("多元适配")).toBeTruthy();
  expect(screen.getByText("代码驱动，灵活接入")).toBeTruthy();
  expect(screen.getByText("在线开发")).toBeTruthy();
  expect(screen.getByText("编辑、测试、运行、日志一体")).toBeTruthy();
  expect(screen.getByText("AI 辅助")).toBeTruthy();
  expect(screen.getByText("生成、修改、调试更高效")).toBeTruthy();
});

it("renders the console shell with catalog, workbench header and the three main tabs", async () => {
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [makeAdapter()] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [] }),
    },
  ]);
  render(<App />);
  await screen.findByTestId("adapter-catalog");
  await selectFirstAdapter();
  expect(screen.getByTestId("workbench-header")).toBeTruthy();
  expect(screen.getByText("编辑")).toBeTruthy();
  expect(screen.getByText("运行设置")).toBeTruthy();
  expect(screen.getByText("执行记录")).toBeTruthy();
});

it("edits metadata and deletes the adapter from the settings drawer", async () => {
  const adapters: Adapter[] = [makeAdapter()];
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: adapters }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [] }),
    },
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}") as { name: string; description: string };
        const updated = { ...adapters[0], name: payload.name, description: payload.description };
        adapters[0] = updated;
        return { body: updated };
      },
    },
    {
      method: "DELETE",
      match: "/api/adapters/1",
      respond: () => {
        adapters.length = 0;
        return { status: 204 };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();

  fireEvent.click(screen.getByTestId("adapter-settings"));
  await screen.findByTestId("adapter-name");
  fireEvent.change(screen.getByTestId("adapter-name"), { target: { value: "renamed" } });
  fireEvent.change(screen.getByTestId("adapter-description"), { target: { value: "new desc" } });
  fireEvent.click(screen.getByTestId("update-details"));
  await screen.findByRole("heading", { name: "renamed" });

  vi.spyOn(window, "confirm").mockReturnValue(true);
  fireEvent.click(screen.getByTestId("delete-adapter"));
  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => url === "/api/adapters/1" && init?.method === "DELETE",
      ),
    ).toBe(true);
  });
  await waitFor(() => {
    expect(screen.queryAllByTestId("adapter-item")).toHaveLength(0);
  });
  expect(screen.getByText("请选择一个 Adapter 进行管理。")).toBeTruthy();
});

// --- M3.1 Review round 1：Monaco 主题 / Catalog 稳定性 / vN 一致性 ----------

function monacoTheme(): string {
  return screen.getByTestId("editor-main").getAttribute("data-monaco-theme") ?? "";
}

it("defaults the Monaco theme to dark and persists switches in localStorage", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch(consoleWithVersionRoutes(adapter, makeVersion()));
  render(<App />);
  await selectFirstAdapter();

  // 默认深色，且主题确实传给了编辑器。
  expect(monacoTheme()).toBe("vs-dark");
  expect(screen.getByTestId("code-editor").getAttribute("data-monaco-theme")).toBe("vs-dark");
  expect(localStorage.getItem(EDITOR_THEME_STORAGE_KEY)).toBeNull();

  fireEvent.click(screen.getByText("浅色"));
  await waitFor(() => {
    expect(monacoTheme()).toBe("light");
  });
  expect(localStorage.getItem(EDITOR_THEME_STORAGE_KEY)).toBe("light");

  fireEvent.click(screen.getByText("跟随系统"));
  await waitFor(() => {
    expect(localStorage.getItem(EDITOR_THEME_STORAGE_KEY)).toBe("system");
  });
});

it("restores the persisted Monaco theme preference after remount", async () => {
  localStorage.setItem(EDITOR_THEME_STORAGE_KEY, "light");
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch(consoleWithVersionRoutes(adapter, makeVersion()));
  render(<App />);
  await selectFirstAdapter();
  expect(monacoTheme()).toBe("light");
});

it("follows the browser color scheme when the Monaco theme preference is 跟随系统", async () => {
  // jsdom has no matchMedia: stub it with a controllable media query list.
  const listeners = new Set<() => void>();
  const media = { matches: false };
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      get matches() {
        return media.matches;
      },
      addEventListener: (_: string, listener: () => void) => {
        listeners.add(listener);
      },
      removeEventListener: (_: string, listener: () => void) => {
        listeners.delete(listener);
      },
    })),
  );
  localStorage.setItem(EDITOR_THEME_STORAGE_KEY, "system");
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch(consoleWithVersionRoutes(adapter, makeVersion()));
  render(<App />);
  await selectFirstAdapter();

  // 系统浅色 → Monaco 浅色；系统切深色后立即跟随。
  expect(monacoTheme()).toBe("light");
  act(() => {
    media.matches = true;
    for (const listener of [...listeners]) {
      listener();
    }
  });
  await waitFor(() => {
    expect(monacoTheme()).toBe("vs-dark");
  });
});

it("shows a JavaScript working copy diff with the matching Monaco mode and dependency label", async () => {
  const adapter = makeAdapter({ language: "javascript", latest_version_id: 10 });
  stubFetch(consoleWithVersionRoutes(adapter, makeVersion({ code: "baseline-code\n" })));
  render(<App />);
  await selectFirstAdapter();

  fireEvent.change(screen.getByTestId("code-editor"), { target: { value: "edited-code\n" } });
  fireEvent.click(screen.getByTestId("working-diff"));

  await screen.findByTestId("version-diff");
  const diff = screen.getByTestId("diff-editor");
  expect(diff.getAttribute("data-monaco-language")).toBe("javascript");
  expect(diff.getAttribute("data-original")).toBe("baseline-code\n");
  expect(diff.getAttribute("data-modified")).toBe("edited-code\n");
  expect(within(screen.getByTestId("version-diff")).getByText("npm 依赖")).toBeTruthy();
});

it("gives repeated Credential Binding controls stable row-specific accessible names", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: "/api/credentials",
      respond: () => ({
        body: [
          {
            id: 1,
            name: "runtime-secret",
            type: "password",
            created_at: "2026-08-11T00:00:00Z",
            updated_at: "2026-08-11T00:00:00Z",
          },
        ],
      }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/credential-bindings",
      respond: () => ({
        body: [
          {
            env_key: "DB_PASSWORD",
            credential_id: 1,
            credential_name: "runtime-secret",
            field: "password",
          },
        ],
      }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByText("凭据绑定"));
  await screen.findByRole("group", { name: "绑定 1" });
  expect(screen.getByRole("textbox", { name: "绑定 1 环境变量" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "绑定 1 凭据" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "绑定 1 字段" })).toBeTruthy();

  fireEvent.click(screen.getByTestId("add-binding"));
  expect(screen.getByRole("group", { name: "绑定 2" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "绑定 2 环境变量" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "绑定 2 凭据" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "绑定 2 字段" })).toBeTruthy();
});

it("manages credentials and package sources from the system settings drawer", async () => {
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    emptyAdaptersRoute,
    {
      method: "GET",
      match: "/api/credentials",
      respond: () => ({
        body: [
          {
            id: 1,
            name: "db-password",
            type: "password",
            created_at: "2026-08-11T00:00:00Z",
            updated_at: "2026-08-11T00:00:00Z",
          },
        ],
      }),
    },
    {
      method: "GET",
      match: "/api/package-sources",
      respond: () => ({
        body: [
          {
            id: 1,
            name: "internal-pypi",
            kind: "pypi",
            index_url: "https://pypi.example.com/simple/",
            is_default: true,
            credential_id: null,
            credential_name: null,
            created_at: "2026-08-11T00:00:00Z",
            updated_at: "2026-08-11T00:00:00Z",
          },
        ],
      }),
    },
    {
      method: "POST",
      match: "/api/package-sources/1/test",
      respond: () => ({ body: { ok: true, status_code: 200, error: null } }),
    },
  ]);
  render(<App />);
  fireEvent.click(await screen.findByTestId("system-settings"));

  // 凭据管理为默认页签：只展示元数据，API 不会回传明文。
  await screen.findByTestId("credentials-panel");
  await screen.findByTestId("credential-row");
  expect(screen.getByTestId("credential-row").textContent).toBe("db-password");
  fireEvent.click(screen.getByTestId("new-credential"));
  expect(screen.getByRole("textbox", { name: "凭据名称" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "凭据类型" })).toBeTruthy();
  expect(screen.getByLabelText("凭据字段 username")).toBeTruthy();
  expect(screen.getByLabelText("凭据字段 password")).toBeTruthy();
  // 依赖源页签：默认源标记 + 可达性测试。
  fireEvent.click(screen.getByText("依赖源"));
  await screen.findByTestId("package-source-row");
  expect(screen.getByTestId("default-source-badge")).toBeTruthy();
  fireEvent.click(screen.getByTestId("new-package-source"));
  expect(screen.getByRole("textbox", { name: "依赖源名称" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "依赖源类型" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "依赖源 Repository URL" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "依赖源凭据" })).toBeTruthy();

  fireEvent.click(screen.getByTestId("test-package-source"));
  await screen.findByTestId("package-source-test-result");
  expect(screen.getByTestId("package-source-test-result").textContent).toContain("可达");
  expect(screen.getByTestId("package-source-test-result").getAttribute("role")).toBe("status");
});

// --- M4 AI Editor -----------------------------------------------------------

const AI_CANDIDATE: AiCandidate = {
  summary: "增加分页处理",
  code: "candidate-code\n",
  requirements: "candidate-dependency\n",
  runtime_config: { page_size: 100 },
  required_secret_keys: ["API_TOKEN", "MISSING_TOKEN"],
};

function aiResponse(message: string, candidate: AiCandidate | null): AiAssistResponse {
  return { message, candidate, provider: "openai", model: "test-model" };
}

function aiBindingsRoute(adapterId: number, envKeys: string[] = []): Route {
  return {
    method: "GET",
    match: `/api/adapters/${adapterId}/credential-bindings`,
    respond: () => ({
      body: envKeys.map((envKey, index) => ({
        env_key: envKey,
        credential_id: index + 1,
        field: "token",
      })),
    }),
  };
}

async function openAiAssistant() {
  fireEvent.click(screen.getByTestId("open-ai-assistant"));
  await screen.findByTestId("ai-assistant-panel");
}

it("expands and collapses the AI panel and blocks send without an Adapter", async () => {
  stubFetch([healthRoute({ status: "ok", database: true }), emptyAdaptersRoute]);
  render(<App />);
  await screen.findByTestId("open-ai-assistant");

  await openAiAssistant();
  expect((screen.getByTestId("ai-message-input") as HTMLTextAreaElement).disabled).toBe(true);
  expect((screen.getByTestId("ai-send") as HTMLButtonElement).disabled).toBe(true);

  fireEvent.click(screen.getByTestId("close-ai-assistant"));
  await screen.findByTestId("open-ai-assistant");
  expect(screen.queryByTestId("ai-assistant-panel")).toBeNull();
});

it.each([
  ["python", "Python 依赖"],
  ["javascript", "npm 依赖"],
  ["java", "Maven 依赖"],
] as const)(
  "sends the %s Working Copy, shows all Candidate diffs, and applies browser-only",
  async (language, dependencyLabel) => {
    const adapter = makeAdapter({ language, latest_version_id: 10 });
    const version = makeVersion({
      code: `base-${language}\n`,
      requirements: `base-dependency-${language}\n`,
      runtime_config: { before: true },
    });
    let assistBody = "";
    const fetchMock = stubFetch([
      ...consoleWithVersionRoutes(adapter, version),
      aiBindingsRoute(1, ["API_TOKEN"]),
      {
        method: "POST",
        match: "/api/adapters/1/ai/assist",
        respond: (body) => {
          assistBody = body ?? "";
          return {
            body: aiResponse("已生成修改候选", AI_CANDIDATE),
          };
        },
      },
    ]);
    render(<App />);
    await selectFirstAdapter();
    await openAiAssistant();

    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "增加分页" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));

    await screen.findByTestId("ai-candidate-summary");
    expect(screen.getByTestId("ai-candidate-summary").textContent).toBe("增加分页处理");
    expect(screen.getByTestId("ai-required-secret-keys").textContent).toContain("API_TOKEN");
    expect(screen.getByTestId("ai-missing-secret-keys").textContent).toContain("MISSING_TOKEN");
    expect(screen.getByTestId("ai-missing-secret-keys").textContent).not.toContain(
      "：API_TOKEN,",
    );

    const payload = JSON.parse(assistBody) as {
      message: string;
      working_copy: {
        code: string;
        requirements: string;
        runtime_config: Record<string, unknown>;
      };
      recent_messages: unknown[];
      base_version_id: number;
    };
    expect(payload).toEqual({
      message: "增加分页",
      working_copy: {
        code: `base-${language}\n`,
        requirements: `base-dependency-${language}\n`,
        runtime_config: { before: true },
      },
      recent_messages: [],
      base_version_id: 10,
    });

    fireEvent.click(screen.getByTestId("ai-view-diff"));
    const diffModal = await screen.findByTestId("version-diff");
    expect(screen.getByTestId("diff-editor").getAttribute("data-monaco-language")).toBe(language);
    expect(within(diffModal).getByText(dependencyLabel)).toBeTruthy();
    fireEvent.click(within(diffModal).getByText(dependencyLabel));
    expect(screen.getByTestId("diff-editor").getAttribute("data-monaco-language")).toBe(
      "plaintext",
    );
    fireEvent.click(within(diffModal).getByText("运行参数"));
    expect(screen.getByTestId("diff-editor").getAttribute("data-monaco-language")).toBe("json");
    fireEvent.click(document.querySelector(".ant-modal-close") as HTMLButtonElement);

    fireEvent.click(screen.getByTestId("ai-apply-candidate"));
    expect(valueOf("code-editor")).toBe("candidate-code\n");
    expect(screen.getByTestId("ai-candidate-applied")).toBeTruthy();
    expect(valueOf("requirements-input")).toBe("candidate-dependency\n");
    fireEvent.click(screen.getByText("运行参数（JSON）"));
    expect(valueOf("runtime-config-input")).toBe('{\n  "page_size": 100\n}');
    expect(screen.getByTestId("dirty-indicator")).toBeTruthy();

    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          init?.method === "POST" &&
          (String(url).endsWith("/versions") ||
            String(url).includes("/executions") ||
            String(url).endsWith("/publish") ||
            String(url).includes("/production/")),
      ),
    ).toBe(false);
  },
);

it("keeps a normal AI Candidate quiet and follows messages only while the user stays near the bottom", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const normalCandidate: AiCandidate = { ...AI_CANDIDATE, required_secret_keys: [] };
  let resolveFirst: ((response: AiAssistResponse) => void) | undefined;
  const firstResponse = new Promise<AiAssistResponse>((resolve) => {
    resolveFirst = resolve;
  });
  let resolveSecond: ((response: AiAssistResponse) => void) | undefined;
  const secondResponse = new Promise<AiAssistResponse>((resolve) => {
    resolveSecond = resolve;
  });
  let assistCalls = 0;
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    aiBindingsRoute(1),
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: async () => {
        assistCalls += 1;
        return {
          body:
            assistCalls === 1
              ? await firstResponse
              : await secondResponse,
        };
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();
  const conversation = screen.getByTestId("ai-conversation") as HTMLDivElement;
  let scrollHeight = 300;
  Object.defineProperty(conversation, "clientHeight", { configurable: true, value: 100 });
  Object.defineProperty(conversation, "scrollHeight", {
    configurable: true,
    get: () => scrollHeight,
  });

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "生成一个正常候选" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-message-user");
  expect(conversation.scrollTop).toBe(300);

  conversation.scrollTop = 10;
  fireEvent.scroll(conversation);
  scrollHeight = 500;
  await act(async () => {
    resolveFirst?.(aiResponse("候选已生成", normalCandidate));
    await firstResponse;
  });
  await screen.findByTestId("ai-candidate-summary");
  expect(conversation.scrollTop).toBe(10);
  expect(screen.getByTestId("ai-candidate-summary").textContent).toBe("增加分页处理");
  expect(screen.getByTestId("ai-view-diff").textContent).toContain("查看修改");
  expect(screen.getByTestId("ai-apply-candidate").textContent?.replace(/\s/g, "")).toBe("应用");
  expect(screen.queryByTestId("ai-candidate-stale")).toBeNull();
  expect(screen.queryByTestId("ai-missing-secret-keys")).toBeNull();

  conversation.scrollTop = 400;
  fireEvent.scroll(conversation);
  scrollHeight = 700;
  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "继续解释" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await waitFor(() => expect(assistCalls).toBe(2));
  expect(conversation.scrollTop).toBe(700);
  scrollHeight = 900;
  await act(async () => {
    resolveSecond?.(aiResponse("第二条回复", null));
    await secondResponse;
  });
  await screen.findByText("第二条回复");
  expect(conversation.scrollTop).toBe(900);

  const inheritedScrollHeight = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "scrollHeight",
  );
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get: () => 950,
  });
  conversation.scrollTop = 10;
  fireEvent.scroll(conversation);
  fireEvent.click(screen.getByTestId("close-ai-assistant"));
  await screen.findByTestId("open-ai-assistant");
  await openAiAssistant();
  expect((screen.getByTestId("ai-conversation") as HTMLDivElement).scrollTop).toBe(950);
  if (inheritedScrollHeight === undefined) {
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollHeight;
  } else {
    Object.defineProperty(HTMLElement.prototype, "scrollHeight", inheritedScrollHeight);
  }
});

it("rejects a runtime config whose JSON number overflows instead of silently sending null", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    aiBindingsRoute(1),
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByText("运行参数（JSON）"));
  fireEvent.change(screen.getByTestId("runtime-config-input"), {
    target: { value: '{"overflow":1e400}' },
  });
  await openAiAssistant();
  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "检查参数" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));

  expect((await screen.findByTestId("ai-panel-error")).textContent).toContain(
    "必须是合法的 JSON 对象",
  );
  expect(
    fetchMock.mock.calls.some(([url]) => String(url).endsWith("/api/adapters/1/ai/assist")),
  ).toBe(false);
});

it("refreshes bindings for a new Candidate while the AI panel remains open", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  let bindingReads = 0;
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: "/api/adapters/1/credential-bindings",
      respond: () => {
        bindingReads += 1;
        return {
          body:
            bindingReads === 1
              ? []
              : [{ env_key: "MISSING_TOKEN", credential_id: 1, field: "token" }],
        };
      },
    },
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: () => ({ body: aiResponse("候选已生成", AI_CANDIDATE) }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();
  await waitFor(() => expect(bindingReads).toBe(1));

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "使用刚绑定的 Secret" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-candidate-summary");

  expect(bindingReads).toBe(2);
  expect(screen.getByTestId("ai-missing-secret-keys").textContent).toContain("API_TOKEN");
  expect(screen.getByTestId("ai-missing-secret-keys").textContent).not.toContain("MISSING_TOKEN");
});

it("marks a Candidate stale after an in-flight edit and requires explicit still-apply", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  let resolveAssist: ((response: AiAssistResponse) => void) | undefined;
  const pendingAssist = new Promise<AiAssistResponse>((resolve) => {
    resolveAssist = resolve;
  });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion({ code: "base-code\n" })),
    aiBindingsRoute(1),
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: async () => ({ body: await pendingAssist }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "修改代码" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-loading");
  fireEvent.change(screen.getByTestId("code-editor"), {
    target: { value: "manual-newer-code\n" },
  });

  await act(async () => {
    resolveAssist?.(aiResponse("候选已生成", AI_CANDIDATE));
    await pendingAssist;
  });
  await screen.findByTestId("ai-candidate-stale");
  expect(valueOf("code-editor")).toBe("manual-newer-code\n");
  expect(screen.getByTestId("ai-apply-candidate").textContent).toContain("仍然应用");

  fireEvent.click(screen.getByTestId("ai-apply-candidate"));
  expect(valueOf("code-editor")).toBe("candidate-code\n");
});

it("clears conversation on Adapter switch and ignores the old Adapter response", async () => {
  const adapterA = makeAdapter({ id: 1, name: "adapter-a" });
  const adapterB = makeAdapter({ id: 2, name: "adapter-b" });
  let resolveAssistA: ((response: AiAssistResponse) => void) | undefined;
  const pendingAssistA = new Promise<AiAssistResponse>((resolve) => {
    resolveAssistA = resolve;
  });
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [adapterA, adapterB] }),
    },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/2/versions", respond: () => ({ body: [] }) },
    aiBindingsRoute(1),
    aiBindingsRoute(2),
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: async () => ({ body: await pendingAssistA }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();
  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "A 的问题" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-message-user");

  fireEvent.click(screen.getAllByTestId("adapter-item")[1]);
  await screen.findByRole("heading", { name: "adapter-b" });
  expect(screen.queryByTestId("ai-message-user")).toBeNull();

  await act(async () => {
    resolveAssistA?.(aiResponse("A 的旧响应", AI_CANDIDATE));
    await pendingAssistA;
  });
  expect(screen.queryByText("A 的旧响应")).toBeNull();
  expect(screen.queryByTestId("ai-candidate")).toBeNull();
});

it("sends at most the latest eight visible role/content messages", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const requestBodies: string[] = [];
  let responseNumber = 0;
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    aiBindingsRoute(1),
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: (body) => {
        requestBodies.push(body ?? "");
        responseNumber += 1;
        return { body: { message: `回答 ${responseNumber}`, candidate: null } };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();

  for (let index = 1; index <= 6; index += 1) {
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: `问题 ${index}` },
    });
    fireEvent.click(screen.getByTestId("ai-send"));
    await waitFor(() => {
      expect(screen.getAllByTestId("ai-message-assistant")).toHaveLength(index);
    });
  }

  const sixth = JSON.parse(requestBodies[5]) as {
    recent_messages: Record<string, unknown>[];
  };
  expect(sixth.recent_messages).toHaveLength(8);
  expect(sixth.recent_messages[0]).toEqual({ role: "user", content: "问题 2" });
  expect(
    sixth.recent_messages.every(
      (message) => Object.keys(message).sort().join(",") === "content,role",
    ),
  ).toBe(true);
});

it.each(["task", "webhook"] as const)(
  "keeps archived %s Adapter read-only without a restore action",
  async (adapterType) => {
    const archived = makeAdapter({
      adapter_type: adapterType,
      archived_at: "2026-08-11T01:00:00Z",
    });
    stubFetch([
      healthRoute({ status: "ok", database: true }),
      { method: "GET", match: "/api/adapters", respond: () => ({ body: [archived] }) },
      { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [] }) },
      aiBindingsRoute(1),
      {
        method: "POST",
        match: "/api/adapters/1/ai/assist",
        respond: () => ({ body: { message: "只读候选", candidate: AI_CANDIDATE } }),
      },
    ]);
    render(<App />);
    await screen.findByTestId("adapter-catalog");
    fireEvent.click(screen.getByText("已归档"));
    fireEvent.click(await screen.findByTestId("adapter-item"));
    await screen.findByTestId("code-editor");
    expect((screen.getByTestId("code-editor") as HTMLTextAreaElement).disabled).toBe(true);
    expect((screen.getByTestId("requirements-input") as HTMLTextAreaElement).disabled).toBe(true);
    fireEvent.click(screen.getByText("运行参数（JSON）"));
    expect((screen.getByTestId("runtime-config-input") as HTMLTextAreaElement).disabled).toBe(true);

    await openAiAssistant();
    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "解释并建议" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));
    await screen.findByTestId("ai-archived-apply-blocked");
    const applyButton = screen.getByTestId("ai-apply-candidate") as HTMLButtonElement;
    expect(applyButton.disabled).toBe(true);
    expect(applyButton.closest(".action-with-reason")?.getAttribute("title")).toContain(
      "只能查看，不能应用",
    );

    fireEvent.click(screen.getByTestId("adapter-settings"));
    await screen.findByTestId("archived-settings-readonly");
    expect((screen.getByTestId("adapter-name") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByTestId("adapter-description") as HTMLInputElement).disabled).toBe(true);
    expect(screen.queryByTestId("restore-adapter")).toBeNull();
    expect(screen.queryByTestId("delete-adapter")).toBeNull();
    expect(screen.queryByTestId("clone-adapter")).toBeNull();
  },
);

it("configures one AI model with manual Model ID, refresh, test, and default reasoning", async () => {
  let refreshBody = "";
  let testBody = "";
  let saveBody = "";
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    emptyAdaptersRoute,
    {
      method: "GET",
      match: "/api/credentials",
      respond: () => ({
        body: [
          { id: 1, name: "ai-token", type: "token", created_at: "", updated_at: "" },
          { id: 2, name: "not-a-token", type: "password", created_at: "", updated_at: "" },
        ],
      }),
    },
    { method: "GET", match: "/api/ai/settings", respond: () => ({ body: null }) },
    {
      method: "POST",
      match: "/api/ai/models/refresh",
      respond: (body) => {
        refreshBody = body ?? "";
        return { body: { models: ["model-from-server"] } };
      },
    },
    {
      method: "POST",
      match: "/api/ai/settings/test",
      respond: (body) => {
        testBody = body ?? "";
        return { body: { ok: true, message: "模型响应可解析" } };
      },
    },
    {
      method: "PUT",
      match: "/api/ai/settings",
      respond: (body) => {
        saveBody = body ?? "";
        return { body: JSON.parse(saveBody) };
      },
    },
  ]);
  render(<App />);
  fireEvent.click(await screen.findByTestId("system-settings"));
  fireEvent.click(await screen.findByText("AI 模型"));
  await screen.findByTestId("ai-model-settings-panel");

  expect(screen.getByTestId("ai-data-boundary-warning").textContent).toContain("Working Copy");
  fireEvent.click(screen.getByText("高级：推理策略（跟随模型默认）"));
  expect(screen.getByTestId("ai-reasoning-mode").textContent).toContain("跟随模型默认");
  expect(screen.queryByTestId("ai-reasoning-effort")).toBeNull();

  fireEvent.change(screen.getByTestId("ai-base-url"), {
    target: { value: "https://models.example.com/v1" },
  });
  fireEvent.change(screen.getByTestId("ai-model-input"), {
    target: { value: "manual-model" },
  });
  fireEvent.click(screen.getByTestId("ai-refresh-models"));
  await waitFor(() => expect(refreshBody).not.toBe(""));
  expect(valueOf("ai-model-input")).toBe("manual-model");
  expect(
    Array.from(screen.getByTestId("ai-model-suggestions").querySelectorAll("option")).map(
      (option) => option.value,
    ),
  ).toEqual(["model-from-server"]);

  fireEvent.click(screen.getByTestId("ai-test-connection"));
  await waitFor(() => expect(testBody).not.toBe(""));
  expect(screen.getByTestId("ai-settings-notice").textContent).toContain("模型响应可解析");

  fireEvent.click(screen.getByTestId("ai-save-settings"));
  await waitFor(() => expect(saveBody).not.toBe(""));
  expect(JSON.parse(saveBody)).toEqual({
    provider: "openai",
    base_url: "https://models.example.com/v1",
    model: "manual-model",
    credential_id: null,
    reasoning_mode: "default",
    reasoning_effort: null,
  });
  expect(JSON.parse(testBody).reasoning_mode).toBe("default");
  expect(JSON.parse(refreshBody).model).toBeUndefined();
});

// --- M5.4.3 Webhook Adapter final user model ---------------------------------

function makeWebhook(overrides: Record<string, unknown> = {}) {
  return {
    adapter_id: 1,
    enabled: false,
    public_id: "a8f3c9d2",
    hook_path: "/api/hooks/a8f3c9d2",
    credential_id: 7,
    credential_name: "hook-token",
    created_at: "2026-08-15T00:00:00Z",
    updated_at: "2026-08-15T00:00:00Z",
    ...overrides,
  };
}

function webhookConsoleRoutes(adapter: Adapter, webhook = makeWebhook()): Route[] {
  return [
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [{ id: 3, name: "hook-worker", status: "online", last_heartbeat: "", capabilities: [adapter.language] }],
      }),
    },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1", respond: () => ({ body: adapter }) },
    { method: "GET", match: "/api/adapters/1/webhook", respond: () => ({ body: webhook }) },
    { method: "GET", match: "/api/credentials", respond: () => ({ body: [{ id: 7, name: "hook-token", type: "token", created_at: "", updated_at: "" }] }) },
  ];
}

it("shows the Webhook starter and only 编辑 / 运行设置 / 调用记录", async () => {
  const adapter = makeAdapter({ adapter_type: "webhook", name: "hook-a", runtime_worker_id: 3 });
  stubFetch(webhookConsoleRoutes(adapter));
  render(<App />);
  await selectFirstAdapter();

  expect(valueOf("code-editor")).toBe(WEBHOOK_STARTER_CODE);
  expect(screen.getByTestId("webhook-workbench-header")).toBeDefined();
  expect(screen.getByRole("tab", { name: "编辑" })).toBeDefined();
  expect(screen.getByRole("tab", { name: "运行设置" })).toBeDefined();
  expect(screen.getByRole("tab", { name: "调用记录" })).toBeDefined();
  expect(document.body.textContent).not.toMatch(/Publish|Published|Production|测试运行|触发器|Cron|Timezone/);
});

it("requests Webhook call history with a server-side trigger filter", async () => {
  const adapter = makeAdapter({
    adapter_type: "webhook",
    latest_version_id: 10,
    runtime_worker_id: 3,
  });
  const legacyManual = makeSummary({ id: 30, trigger: "manual" });
  const webhookCall = makeSummary({ id: 31, trigger: "webhook" });
  const historyUrls: string[] = [];
  stubFetch([
    ...webhookConsoleRoutes(adapter),
    {
      method: "GET",
      match: /\/api\/adapters\/1\/executions\?/,
      respond: (_body, url) => {
        historyUrls.push(url);
        return {
          body: {
            items: url.includes("trigger=webhook")
              ? [webhookCall]
              : [legacyManual, webhookCall],
            next_before_id: null,
          },
        };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "调用记录" }));

  expect(await screen.findAllByTestId("history-row")).toHaveLength(1);
  expect(historyUrls).toHaveLength(1);
  expect(historyUrls[0]).toContain("trigger=webhook");
  expect(document.body.textContent).toContain("#31");
  expect(document.body.textContent).not.toContain("#30");
});

it("edits only the URL path, saves Worker and Token, then starts receiving", async () => {
  let adapter = makeAdapter({
    adapter_type: "webhook",
    latest_version_id: 10,
    runtime_worker_id: 3,
  });
  let webhook = makeWebhook();
  const fetchMock = stubFetch([
    ...webhookConsoleRoutes(adapter, webhook),
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: (body) => {
        adapter = { ...adapter, runtime_worker_id: JSON.parse(body ?? "{}").runtime_worker_id };
        return { body: adapter };
      },
    },
    {
      method: "PUT",
      match: "/api/adapters/1/webhook",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}");
        webhook = { ...webhook, ...payload, hook_path: `/api/hooks/${payload.public_id}` };
        if (payload.enabled) adapter = { ...adapter, runtime_locked: true };
        return { body: webhook };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  await screen.findByTestId("webhook-run-settings");

  expect(valueOf("webhook-prefix")).toBe(`${window.location.origin}/api/hooks/`);
  expect((screen.getByTestId("webhook-prefix") as HTMLInputElement).readOnly).toBe(true);
  fireEvent.change(screen.getByTestId("webhook-public-id"), { target: { value: "receive-sys1-data" } });
  fireEvent.click(screen.getByTestId("webhook-save"));
  await screen.findByText("运行设置已保存。");
  fireEvent.click(screen.getByTestId("webhook-start"));
  await screen.findByText("已开启接收。");

  const payloads = fetchMock.mock.calls
    .filter(([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT")
    .map(([, init]) => JSON.parse(String(init?.body)));
  expect(payloads).toEqual([
    { enabled: false, public_id: "receive-sys1-data", credential_id: 7 },
    { enabled: true, public_id: "receive-sys1-data", credential_id: 7 },
  ]);
  expect(screen.getByTestId("webhook-url")).toHaveProperty(
    "value",
    `${window.location.origin}/api/hooks/receive-sys1-data`,
  );
});

it("rejects an invalid path locally and renders the stable path-in-use message", async () => {
  const adapter = makeAdapter({
    adapter_type: "webhook",
    latest_version_id: 10,
    runtime_worker_id: 3,
  });
  const fetchMock = stubFetch([
    ...webhookConsoleRoutes(adapter),
    {
      method: "PUT",
      match: "/api/adapters/1/webhook",
      respond: () => ({
        status: 409,
        body: { detail: { code: "webhook_path_in_use", message: "conflict" } },
      }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  await screen.findByTestId("webhook-run-settings");
  fireEvent.change(screen.getByTestId("webhook-public-id"), { target: { value: "INVALID_PATH" } });
  expect(screen.getByTestId("webhook-path-invalid")).toBeDefined();
  expect((screen.getByTestId("webhook-save") as HTMLButtonElement).disabled).toBe(true);
  expect(fetchMock.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);

  fireEvent.change(screen.getByTestId("webhook-public-id"), { target: { value: "a8f3c9d2" } });
  fireEvent.click(screen.getByTestId("webhook-start"));
  expect((await screen.findByRole("alert")).textContent).toContain(
    "Webhook 地址 a8f3c9d2 当前正在被另一个运行中的 Adapter 使用",
  );
});

it("preserves an unchanged legacy Webhook path but validates it once edited", async () => {
  const adapter = makeAdapter({
    adapter_type: "webhook",
    latest_version_id: 10,
    runtime_worker_id: 3,
  });
  const legacyPath = "Legacy_Path_ABC123";
  let webhook = makeWebhook({
    public_id: legacyPath,
    hook_path: `/api/hooks/${legacyPath}`,
  });
  const fetchMock = stubFetch([
    ...webhookConsoleRoutes(adapter, webhook),
    {
      method: "PUT",
      match: "/api/adapters/1/webhook",
      respond: (body) => {
        webhook = { ...webhook, ...JSON.parse(body ?? "{}") };
        return { body: webhook };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  await screen.findByTestId("webhook-run-settings");

  expect(screen.getByTestId("webhook-path-legacy")).toBeDefined();
  expect(screen.queryByTestId("webhook-path-invalid")).toBeNull();
  fireEvent.change(screen.getByTestId("webhook-public-id"), {
    target: { value: "Another_Invalid_Path" },
  });
  expect(screen.queryByTestId("webhook-path-legacy")).toBeNull();
  expect(screen.getByTestId("webhook-path-invalid")).toBeDefined();
  expect((screen.getByTestId("webhook-save") as HTMLButtonElement).disabled).toBe(true);

  fireEvent.change(screen.getByTestId("webhook-public-id"), { target: { value: legacyPath } });
  fireEvent.click(screen.getByTestId("webhook-start"));
  await screen.findByText("已开启接收。");
  const startCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT",
  );
  expect(JSON.parse(String(startCall?.[1]?.body))).toEqual({
    enabled: true,
    public_id: legacyPath,
    credential_id: 7,
  });
});

it("stops receiving without unlocking an active call or exposing the Token", async () => {
  const adapter = makeAdapter({
    adapter_type: "webhook",
    latest_version_id: 10,
    runtime_worker_id: 3,
    runtime_locked: true,
    running_execution_id: 91,
  });
  let webhook = makeWebhook({ enabled: true });
  const fetchMock = stubFetch([
    ...webhookConsoleRoutes(adapter, webhook),
    {
      method: "PUT",
      match: "/api/adapters/1/webhook",
      respond: (body) => {
        webhook = { ...webhook, ...JSON.parse(body ?? "{}") };
        return { body: webhook };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  await screen.findByTestId("webhook-run-settings");
  expect((screen.getByTestId("webhook-public-id") as HTMLInputElement).disabled).toBe(true);
  expect(document.body.textContent).not.toContain("real-hook-secret");
  fireEvent.click(screen.getByTestId("webhook-stop"));
  await screen.findByText("已停止接收；已有调用会继续运行到终态。");
  expect(screen.getByTestId("webhook-runtime-locked")).toBeDefined();
  const stopCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT",
  );
  expect(JSON.parse(String(stopCall?.[1]?.body))).toEqual({
    enabled: false,
    public_id: "a8f3c9d2",
    credential_id: 7,
  });
});
