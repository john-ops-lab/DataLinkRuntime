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

// M3.2：Publish 走确认框，打开时拉取只读门禁评估。
const publishGateRoute = (
  adapterId: number,
  versionId: number,
  gate: { allowed: boolean; reason: string | null; last_test: unknown } = {
    allowed: true,
    reason: null,
    last_test: null,
  },
): Route => ({
  method: "GET",
  match: `/api/adapters/${adapterId}/versions/${versionId}/publish-gate`,
  respond: () => ({ body: gate }),
});

function makeAdapter(overrides: Partial<Adapter> = {}): Adapter {
  return {
    id: 1,
    name: "adapter-a",
    description: "",
    language: "python",
    adapter_type: "task",
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

it("keeps normal Catalog rows quiet, flags offline Workers, and searches descriptions", async () => {
  const normal = makeAdapter({
    id: 1,
    name: "orders-sync",
    description: "同步订单与账单",
    language: "javascript",
    published_version_id: 30,
    published_version_seq: 3,
    production_worker_id: 1,
    production_state: "running",
    running_execution_id: 90,
    running_version_id: 30,
    running_version_seq: 3,
  });
  // M5.1: the previous lifecycle ended failed, but the entry is running without
  // an active Execution: a legal “已开启 · 空闲” row, never “状态异常”.
  const previouslyFailed = makeAdapter({
    id: 2,
    name: "nightly-import",
    description: "夜间失败数据导入",
    published_version_id: 20,
    published_version_seq: 2,
    production_worker_id: 2,
    production_state: "running",
    production_version_id: 20,
    production_version_seq: 2,
    running_execution_id: null,
    last_production_execution_id: 91,
    last_production_execution_status: "failed",
    last_production_version_id: 20,
    last_production_version_seq: 2,
  });
  const stopping = makeAdapter({
    id: 3,
    name: "stopping-export",
    description: "停止中的长任务",
    published_version_id: 30,
    published_version_seq: 3,
    production_worker_id: 1,
    production_state: "stopped",
    running_execution_id: 92,
    running_version_id: 30,
    running_version_seq: 3,
  });
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [normal, previouslyFailed, stopping] }),
    },
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [
          { id: 1, name: "worker-main", status: "online", last_heartbeat: "", capabilities: ["javascript"] },
          { id: 2, name: "worker-backup", status: "offline", last_heartbeat: "", capabilities: ["python"] },
        ],
      }),
    },
    { method: "GET", match: "/api/adapters/2/versions", respond: () => ({ body: [] }) },
  ]);

  render(<App />);
  const rows = await screen.findAllByTestId("adapter-item");
  expect(rows[0].querySelector(".catalog-item-sub")?.textContent).toBe(
    "JavaScript · 生产运行 v3",
  );
  expect(rows[0].querySelector(".catalog-item-sub")?.textContent).not.toContain("worker-main");
  expect(rows[0].getAttribute("title")).toContain("同步订单与账单");
  await waitFor(() => {
    expect(rows[0].getAttribute("title")).toContain("Worker worker-main");
    expect(rows[1].querySelector(".catalog-item-sub")?.textContent).toContain("Worker 离线");
    expect(rows[1].querySelector(".catalog-item-sub")?.textContent).toContain(
      "生产入口已开启 · 空闲 v2",
    );
    expect(rows[1].querySelector(".catalog-item-sub")?.textContent).not.toContain("状态异常");
  });
  expect(rows[1].querySelector(".catalog-item-attention")).not.toBeNull();
  expect(rows[2].querySelector(".catalog-item-sub")?.textContent).toContain("停止中 v3");
  expect(rows[2].querySelector(".catalog-item-sub")?.textContent).toContain(
    "等待 Execution #92 完成",
  );
  expect(
    fetchMock.mock.calls.filter(([url]) => String(url) === "/api/workers"),
  ).toHaveLength(1);

  fireEvent.change(screen.getByTestId("adapter-search"), {
    target: { value: "失败数据" },
  });
  const [descriptionMatch] = screen.getAllByTestId("adapter-item");
  expect(descriptionMatch.querySelector(".catalog-item-name")?.textContent).toContain(
    "nightly-import",
  );
  fireEvent.click(descriptionMatch);
  // The old failure stays visible as an independent execution-result notice,
  // without any “先 Stop 再 Start” lifecycle guidance.
  const failureNotice = await screen.findByTestId("production-last-failure");
  expect(failureNotice.textContent).toContain("Execution #91");
  expect(failureNotice.textContent).toContain("无需 Stop → Start");
  expect(failureNotice.textContent).not.toContain("先 Stop 关闭当前生产入口");
  expect(failureNotice.textContent).not.toContain("如需恢复再 Start");
  expect(screen.getByTestId("production-state").textContent).toBe("生产：已启动");
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

  expect(valueOf("code-editor")).toBe(STARTER_CODE);
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

  await screen.findByTestId("latest-badge");
  const saveCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url).endsWith("/versions") && init?.method === "POST",
  );
  expect(JSON.parse(String(saveCall?.[1]?.body))).toEqual({
    code: "def handle(context, input):\n    return {'done': True}\n",
    requirements: "requests==2.32.0",
    runtime_config: { batch: 10 },
  });
  // The header version selector now shows the acknowledged version.
  expect(screen.getByTestId("version-selector").textContent).toContain("v1");
  expect(await screen.findByText("已保存为 v1")).toBeTruthy();
});

it("loads the snapshot of a selected historical version", async () => {
  const adapter = makeAdapter({ latest_version_id: 11 });
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [adapter] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({
        body: [
          { id: 11, adapter_id: 1, seq: 2, created_at: "2026-08-11T00:00:00Z" },
          { id: 10, adapter_id: 1, seq: 1, created_at: "2026-08-11T00:00:00Z" },
        ],
      }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions/11",
      respond: () => ({ body: makeVersion({ id: 11, seq: 2, code: "code-v2" }) }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions/10",
      respond: () => ({ body: makeVersion({ id: 10, seq: 1, code: "code-v1" }) }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  expect(valueOf("code-editor")).toBe("code-v2");

  // Version switching moved to the header dropdown menu.
  fireEvent.click(screen.getByTestId("version-selector"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "v1" }));
  await waitFor(() => {
    expect(valueOf("code-editor")).toBe("code-v1");
  });
  expect(screen.getByTestId("version-seq").textContent).toBe("v1");
});

it("updates the published badge after publishing the selected version", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  let published: Adapter = adapter;
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [published] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [{ id: 10, adapter_id: 1, seq: 1, created_at: "" }] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions/10",
      respond: () => ({ body: makeVersion() }),
    },
    publishGateRoute(1, 10),
    {
      method: "POST",
      match: "/api/adapters/1/versions/10/publish",
      respond: () => {
        published = { ...adapter, published_version_id: 10 };
        return { body: published };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  expect(screen.queryByTestId("published-badge")).toBeNull();

  // M3.2：发布需先在确认框中确认（门禁通过后才可点）。
  fireEvent.click(screen.getByTestId("publish-version"));
  await screen.findByTestId("publish-gate-ok");
  fireEvent.click(screen.getByTestId("confirm-publish"));
  await screen.findByTestId("published-badge");
  expect(screen.getByTestId("latest-badge")).toBeTruthy();
});

it("keeps Publish available for the selected saved version while the Working Copy is dirty", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  let published: Adapter = adapter;
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [published] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [{ id: 10, adapter_id: 1, seq: 1, created_at: "" }] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions/10",
      respond: () => ({ body: makeVersion() }),
    },
    publishGateRoute(1, 10),
    {
      method: "POST",
      match: "/api/adapters/1/versions/10/publish",
      respond: () => {
        published = { ...adapter, published_version_id: 10 };
        return { body: published };
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.change(screen.getByTestId("code-editor"), { target: { value: "unsaved edit" } });
  await screen.findByTestId("dirty-indicator");

  const publishButton = screen.getByTestId("publish-version") as HTMLButtonElement;
  expect(publishButton.disabled).toBe(false);
  fireEvent.click(publishButton);
  await screen.findByTestId("publish-gate-ok");
  fireEvent.click(screen.getByTestId("confirm-publish"));
  await screen.findByTestId("published-badge");

  const publishCall = fetchMock.mock.calls.find(
    ([url, init]) => init?.method === "POST" && String(url).endsWith("/versions/10/publish"),
  );
  expect(publishCall).toBeDefined();
  expect(valueOf("code-editor")).toBe("unsaved edit");
  expect(screen.getByTestId("dirty-indicator")).toBeTruthy();
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
  expect(valueOf("code-editor")).toBe(STARTER_CODE);

  fireEvent.click(screen.getAllByTestId("adapter-item")[1]);
  await screen.findByTestId("error-banner");

  // adapter-b is selected, but adapter-a's content must not leak into it and
  // Save must stay disabled because nothing loaded successfully for adapter-b.
  expect(screen.getByRole("heading", { name: "adapter-b" })).toBeTruthy();
  expect(valueOf("code-editor")).not.toBe(STARTER_CODE);
  const saveButton = screen.getByTestId("save-version") as HTMLButtonElement;
  const publishButton = screen.getByTestId("publish-version") as HTMLButtonElement;
  expect(saveButton.disabled).toBe(true);
  expect(publishButton.disabled).toBe(true);
  expect(saveButton.closest(".action-with-reason")?.getAttribute("title")).toContain(
    "版本内容尚未就绪",
  );
  expect(publishButton.closest(".action-with-reason")?.getAttribute("title")).toContain(
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
  expect(valueOf("code-editor")).toBe(STARTER_CODE);

  // Now the stale adapter-a response resolves; it must not overwrite b's state.
  resolveStale?.([{ id: 10, adapter_id: 1, seq: 1, created_at: "2026-08-11T00:00:00Z" }]);
  await waitFor(() => {
    expect(staleDelivered).toBe(true);
  });
  // Give any (broken) stale continuation a chance to commit before asserting.
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(screen.getByRole("heading", { name: "adapter-b" })).toBeTruthy();
  expect(valueOf("code-editor")).toBe(STARTER_CODE);
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
  expect(screen.getByTestId("latest-badge")).toBeTruthy();
  expect(screen.getByTestId("version-selector").textContent).toContain("v1");
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

it("locks navigation while Publish is in flight so a late response cannot mix adapters", async () => {
  let resolvePublish: ((value: Adapter) => void) | undefined;
  const publishResponse = new Promise<Adapter>((resolve) => {
    resolvePublish = resolve;
  });
  const adapterA = makeAdapter({ latest_version_id: 10 });
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [adapterA, makeAdapter({ id: 2, name: "adapter-b" })] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({ body: [{ id: 10, adapter_id: 1, seq: 1, created_at: "" }] }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions/10",
      respond: () => ({ body: makeVersion({ id: 10, code: "code-a" }) }),
    },
    publishGateRoute(1, 10),
    {
      method: "POST",
      match: "/api/adapters/1/versions/10/publish",
      respond: async () => ({ body: await publishResponse }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  expect(valueOf("code-editor")).toBe("code-a");

  // Publish(A) starts in the confirmation dialog and stays pending.
  fireEvent.click(screen.getByTestId("publish-version"));
  await screen.findByTestId("publish-gate-ok");
  fireEvent.click(screen.getByTestId("confirm-publish"));
  await waitFor(() => {
    expect((screen.getByTestId("publish-version") as HTMLButtonElement).disabled).toBe(true);
  });

  // Attempted switch to adapter-b while the mutation is in flight: the list
  // item is disabled and the handler rejects navigation, so B never loads.
  const itemB = screen.getAllByTestId("adapter-item")[1];
  expect((itemB as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(itemB);
  expect(screen.getByRole("heading", { name: "adapter-a" })).toBeTruthy();
  expect(
    fetchMock.mock.calls.some(([url]) => String(url).startsWith("/api/adapters/2")),
  ).toBe(false);

  // The late Publish(A) response commits against adapter-a only.
  resolvePublish?.({ ...adapterA, published_version_id: 10 });
  await screen.findByTestId("published-badge");
  expect(screen.getByRole("heading", { name: "adapter-a" })).toBeTruthy();
  expect(valueOf("code-editor")).toBe("code-a");
});

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
  expect((screen.getByTestId("version-selector") as HTMLButtonElement).disabled).toBe(true);

  // Save completes with exactly the snapshot that existed when it started.
  const savedVersion = makeVersion({ code: "edited code", requirements: "", runtime_config: {} });
  versions.push(savedVersion);
  resolveSave?.(savedVersion);
  await screen.findByTestId("latest-badge");

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
      respond: () => ({ body: [] }),
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
  await screen.findByTestId("latest-badge");
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

async function openTestRunTab() {
  fireEvent.click(screen.getByText("测试运行"));
  await screen.findByTestId("run-test");
}

it("runs a test bound to the selected version and follows the execution via SSE", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const pending = makeExecution({ status: "pending" });
  const running = makeExecution({ status: "running", worker_id: 3, stdout: "step 1\n" });
  const succeeded = makeExecution({
    status: "succeeded",
    worker_id: 3,
    output: { ok: true },
    stdout: "done\n",
    started_at: "2026-08-11T00:00:01Z",
    ended_at: "2026-08-11T00:00:02Z",
    duration_ms: 42,
  });
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [
          { id: 3, name: "worker-01", status: "online", last_heartbeat: "", capabilities: ["python"] },
        ],
      }),
    },
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => ({ status: 202, body: pending }),
    },
    {
      method: "GET",
      match: "/api/executions/5/events",
      respond: () => ({
        // pending panel state first, then running, then the terminal status.
        stream:
          `event: execution\ndata: ${JSON.stringify(running)}\n\n` +
          `event: execution\ndata: ${JSON.stringify(succeeded)}\n\n`,
      }),
    },
    { method: "GET", match: "/api/executions/5", respond: () => ({ body: succeeded }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  const headerText = screen.getByTestId("workbench-header").textContent ?? "";
  expect(headerText).toContain("adapter-a");
  expect(headerText).toContain("Python");
  expect(headerText).toContain("v1");
  await openTestRunTab();
  fireEvent.change(screen.getByTestId("test-input"), { target: { value: '{"k": 1}' } });
  // Duplicate clicks must not create a second execution.
  fireEvent.click(screen.getByTestId("run-test"));
  fireEvent.click(screen.getByTestId("run-test"));

  // The stream ends in a terminal status, so the final detail is reloaded.
  await waitFor(() => {
    expect(screen.getByTestId("execution-status").textContent).toBe("成功");
  });
  expect(screen.getByTestId("execution-id").textContent).toBe("Execution #5");
  expect(screen.getByTestId("execution-worker").textContent).toContain("worker-01");
  expect(screen.getByTestId("execution-worker").textContent).toContain("#3");

  // Explicit version binding: the create request carries the selected version.
  const createCalls = fetchMock.mock.calls.filter(
    ([url, init]) => String(url) === "/api/adapters/1/executions" && init?.method === "POST",
  );
  expect(createCalls).toHaveLength(1);
  expect(JSON.parse(String(createCalls[0][1]?.body))).toEqual({
    input: { k: 1 },
    version_id: 10,
  });

  // SSE auth contract: Bearer header only, the token never enters the URL.
  const sseCall = fetchMock.mock.calls.find(([url]) => String(url) === "/api/executions/5/events");
  expect(sseCall).toBeTruthy();
  expect(String(sseCall?.[0])).not.toContain("test-admin-token");
  const headers = sseCall?.[1]?.headers as Record<string, string>;
  expect(headers.Authorization).toBe("Bearer test-admin-token");

  // stdout tab shows the streamed log content.
  fireEvent.click(screen.getByText("stdout"));
  await waitFor(() => {
    expect(screen.getByTestId("stdout-view").textContent).toContain("done");
  });
});

it("shows size and preview instead of the full output when it was truncated", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const pending = makeExecution({ status: "pending" });
  const truncated = makeExecution({
    status: "succeeded",
    output: null,
    output_size: 600_000,
    output_truncated: true,
    output_preview: "preview head...",
    started_at: "2026-08-11T00:00:01Z",
    ended_at: "2026-08-11T00:00:02Z",
    duration_ms: 42,
  });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => ({ status: 202, body: pending }),
    },
    {
      method: "GET",
      match: "/api/executions/5/events",
      respond: () => ({
        stream: `event: execution\ndata: ${JSON.stringify(truncated)}\n\n`,
      }),
    },
    { method: "GET", match: "/api/executions/5", respond: () => ({ body: truncated }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await openTestRunTab();
  fireEvent.click(screen.getByTestId("run-test"));

  await waitFor(() => {
    expect(screen.getByTestId("execution-status").textContent).toBe("成功");
  });
  const notice = await screen.findByTestId("output-truncated");
  expect(notice.textContent).toContain("600000");
  expect(screen.getByTestId("output-preview").textContent).toContain("preview head...");
  expect(screen.queryByTestId("output-content")).toBeNull();
});

it("blocks test runs while the editor has unsaved changes", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => {
        throw new Error("execution must not be created while the editor is dirty");
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await openTestRunTab();
  fireEvent.change(screen.getByTestId("code-editor"), { target: { value: "edited" } });

  // The run button is disabled while dirty; even a forced click must not
  // reach the API (the business guard stays as defense in depth).
  const runButton = screen.getByTestId("run-test") as HTMLButtonElement;
  expect(runButton.disabled).toBe(true);
  const reasonTarget = runButton.closest(".action-with-reason");
  expect(reasonTarget?.getAttribute("title")).toContain("请先使用顶部“保存新版本”");
  expect(reasonTarget?.getAttribute("aria-label")).toContain("运行测试不可用");
  fireEvent.click(runButton);

  expect(screen.queryByTestId("error-banner")).toBeNull();
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/executions" && init?.method === "POST",
    ),
  ).toBe(false);
  expect(screen.getByText(/存在未保存修改/)).toBeTruthy();
  fireEvent.click(screen.getByTestId("return-to-edit"));
  expect(screen.getByRole("tab", { name: "编辑" }).getAttribute("aria-selected")).toBe("true");
});

it("warns about an offline Worker snapshot without creating a stale client-side Test gate", async () => {
  const adapter = makeAdapter({
    latest_version_id: 10,
    published_version_id: 10,
    published_version_seq: 1,
    production_worker_id: 3,
  });
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [
          { id: 3, name: "worker-offline", status: "offline", last_heartbeat: "", capabilities: ["python"] },
        ],
      }),
    },
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => ({
        status: 202,
        body: makeExecution({ id: 93, worker_id: 3, status: "succeeded" }),
      }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  expect((await screen.findByTestId("header-production-worker-warning")).textContent).toContain(
    "worker-offline 最近状态为离线",
  );
  await openTestRunTab();
  const runButton = screen.getByTestId("run-test") as HTMLButtonElement;
  expect(runButton.disabled).toBe(false);
  expect(screen.getByTestId("test-worker-offline-warning").textContent).toContain(
    "worker-offline 最近状态为离线",
  );
  expect(screen.getByTestId("test-worker-offline-warning").textContent).toContain("后端");
  fireEvent.click(runButton);
  await waitFor(() => {
    expect(screen.getByTestId("execution-status").textContent).toBe("成功");
  });
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) =>
        String(url) === "/api/adapters/1/executions" && init?.method === "POST",
    ),
  ).toBe(true);
});

it("blocks test runs when the input is not valid JSON", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => {
        throw new Error("execution must not be created for invalid input");
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await openTestRunTab();
  fireEvent.change(screen.getByTestId("test-input"), { target: { value: "{oops" } });
  fireEvent.click(screen.getByTestId("run-test"));

  await screen.findByTestId("error-banner");
  expect(screen.getByTestId("error-banner").textContent).toContain("Input 必须是合法 JSON");
});

it("lists execution history with cursor pagination and opens the detail drawer", async () => {
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
  stubFetch([
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

it("converges to the final result when SSE ends without a terminal event", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const pending = makeExecution({ status: "pending" });
  const running = makeExecution({ status: "running", worker_id: 3, stdout: "step 1\n" });
  const succeeded = makeExecution({
    status: "succeeded",
    worker_id: 3,
    output: { ok: true },
    stdout: "done\n",
    started_at: "2026-08-11T00:00:01Z",
    ended_at: "2026-08-11T00:00:02Z",
    duration_ms: 42,
  });
  let detailCalls = 0;
  FALLBACK_POLICY.pollIntervalMs = 50;
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => ({ status: 202, body: pending }),
    },
    {
      method: "GET",
      match: "/api/executions/5/events",
      // Only a running event, then an unexpected EOF (Control restart, proxy
      // drop, nginx read timeout): the UI must not stay stuck on running.
      respond: () => ({
        stream: `event: execution\ndata: ${JSON.stringify(running)}\n\n`,
      }),
    },
    {
      method: "GET",
      match: "/api/executions/5",
      respond: () => {
        detailCalls += 1;
        // The first fallback GET still sees running; the next poll converges.
        return { body: detailCalls === 1 ? running : succeeded };
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await openTestRunTab();
  fireEvent.click(screen.getByTestId("run-test"));

  await screen.findByTestId("execution-status");
  expect(screen.getByTestId("execution-status").textContent).toBe("运行中");

  await waitFor(
    () => {
      expect(screen.getByTestId("execution-status").textContent).toBe("成功");
    },
    { timeout: 5000 },
  );
  expect(detailCalls).toBeGreaterThanOrEqual(2);
});

it("falls back to the authoritative result when the SSE connection fails at start", async () => {
  // M3 second review I2: an initial connect failure / non-2xx answer (e.g.
  // 502 while Control restarts) must enter the same fallback path as a
  // mid-stream EOF, and transient GET failures must keep retrying.
  const adapter = makeAdapter({ latest_version_id: 10 });
  const pending = makeExecution({ status: "pending" });
  const running = makeExecution({ status: "running", worker_id: 3 });
  const succeeded = makeExecution({
    status: "succeeded",
    worker_id: 3,
    output: { ok: true },
    stdout: "done\n",
    started_at: "2026-08-11T00:00:01Z",
    ended_at: "2026-08-11T00:00:02Z",
    duration_ms: 42,
  });
  let detailCalls = 0;
  FALLBACK_POLICY.pollIntervalMs = 50;
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => ({ status: 202, body: pending }),
    },
    {
      method: "GET",
      match: "/api/executions/5/events",
      // Control restarts right as the stream connects: HTTP 502, no body.
      respond: () => ({ status: 502 }),
    },
    {
      method: "GET",
      match: "/api/executions/5",
      respond: () => {
        detailCalls += 1;
        if (detailCalls === 1) {
          throw new Error("Control still restarting");
        }
        return { body: detailCalls === 2 ? running : succeeded };
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await openTestRunTab();
  fireEvent.click(screen.getByTestId("run-test"));

  // The transient GET failure must not stop the bounded retry loop.
  await waitFor(
    () => {
      expect(screen.getByTestId("execution-status").textContent).toBe("成功");
    },
    { timeout: 5000 },
  );
  expect(detailCalls).toBeGreaterThanOrEqual(3);
});

it("shows a stale-state notice when the fallback budget is exhausted", async () => {
  // M3 second review I2: reaching the polling cap without a terminal status
  // must never silently keep a seemingly live running state.
  const adapter = makeAdapter({ latest_version_id: 10 });
  const pending = makeExecution({ status: "pending" });
  const running = makeExecution({ status: "running", worker_id: 3 });
  FALLBACK_POLICY.pollIntervalMs = 20;
  FALLBACK_POLICY.maxPolls = 2;
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => ({ status: 202, body: pending }),
    },
    {
      method: "GET",
      match: "/api/executions/5/events",
      respond: () => ({
        stream: `event: execution\ndata: ${JSON.stringify(running)}\n\n`,
      }),
    },
    { method: "GET", match: "/api/executions/5", respond: () => ({ body: running }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await openTestRunTab();
  fireEvent.click(screen.getByTestId("run-test"));

  const notice = await screen.findByTestId("fallback-exhausted", {}, { timeout: 5000 });
  expect(notice.textContent).toContain("实时连接已断开");
  // The last known state stays visible, but clearly marked as possibly stale.
  expect(screen.getByTestId("execution-status").textContent).toBe("运行中");
});

it("never lets a slow fallback GET of an old run overwrite a newer run", async () => {
  // M3 second review I3: Execution A's fallback GET is still in flight when
  // the user starts Execution B; A's late response must not clobber B.
  const adapter = makeAdapter({ latest_version_id: 10 });
  const pendingA = makeExecution({ id: 5, status: "pending" });
  const runningA = makeExecution({ id: 5, status: "running", worker_id: 3 });
  const pendingB = makeExecution({ id: 6, status: "pending" });
  const succeededB = makeExecution({
    id: 6,
    status: "succeeded",
    worker_id: 3,
    output: { ok: true },
    stdout: "b done\n",
    started_at: "2026-08-11T00:00:01Z",
    ended_at: "2026-08-11T00:00:02Z",
    duration_ms: 42,
  });
  let gateRelease: () => void = () => {};
  const gateA = new Promise<void>((resolve) => {
    gateRelease = resolve;
  });
  let aDetailCalls = 0;
  let postCalls = 0;
  FALLBACK_POLICY.pollIntervalMs = 50;
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => {
        postCalls += 1;
        return { status: 202, body: postCalls === 1 ? pendingA : pendingB };
      },
    },
    {
      method: "GET",
      match: "/api/executions/5/events",
      // A dies with an unexpected EOF before any terminal event.
      respond: () => ({
        stream: `event: execution\ndata: ${JSON.stringify(runningA)}\n\n`,
      }),
    },
    {
      method: "GET",
      match: "/api/executions/6/events",
      // B finishes normally over SSE.
      respond: () => ({
        stream: `event: execution\ndata: ${JSON.stringify(succeededB)}\n\n`,
      }),
    },
    {
      method: "GET",
      match: "/api/executions/5",
      respond: async () => {
        aDetailCalls += 1;
        await gateA; // A's fallback GET is very slow
        return { body: runningA };
      },
    },
    { method: "GET", match: "/api/executions/6", respond: () => ({ body: succeededB }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await openTestRunTab();
  fireEvent.click(screen.getByTestId("run-test"));
  await screen.findByTestId("execution-id");
  expect(screen.getByTestId("execution-id").textContent).toBe("Execution #5");

  // Wait until A's fallback GET is actually in flight, then start B.
  await waitFor(() => {
    expect(aDetailCalls).toBe(1);
  });
  fireEvent.click(screen.getByTestId("run-test"));
  await waitFor(() => {
    expect(screen.getByTestId("execution-status").textContent).toBe("成功");
  });
  expect(screen.getByTestId("execution-id").textContent).toBe("Execution #6");

  // A's slow response arrives last: it must be dropped, not committed.
  gateRelease();
  await new Promise((resolve) => setTimeout(resolve, 100));
  expect(screen.getByTestId("execution-id").textContent).toBe("Execution #6");
  expect(screen.getByTestId("execution-status").textContent).toBe("成功");
});

it("marks the live log as truncated as soon as a log_snapshot says so", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const pending = makeExecution({ status: "pending" });
  const running = makeExecution({ status: "running", worker_id: 3 });
  const succeeded = makeExecution({
    status: "succeeded",
    worker_id: 3,
    stdout: "head kept\n",
    stdout_truncated: true,
    started_at: "2026-08-11T00:00:01Z",
    ended_at: "2026-08-11T00:00:02Z",
    duration_ms: 42,
  });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => ({ status: 202, body: pending }),
    },
    {
      method: "GET",
      match: "/api/executions/5/events",
      respond: () => ({
        // The snapshot arrives while the Execution is still running; the
        // terminal event follows in a later chunk.
        streamChunks: [
          `event: execution\ndata: ${JSON.stringify(running)}\n\n` +
            `event: log_snapshot\ndata: ${JSON.stringify({
              stream: "stdout",
              content: "head kept\n",
              truncated: true,
            })}\n\n`,
          `event: execution\ndata: ${JSON.stringify(succeeded)}\n\n`,
        ],
      }),
    },
    { method: "GET", match: "/api/executions/5", respond: () => ({ body: succeeded }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await openTestRunTab();
  fireEvent.click(screen.getByTestId("run-test"));
  await screen.findByTestId("execution-id");
  fireEvent.click(screen.getByText("stdout"));

  // While still running, the snapshot content and the truncation warning
  // must both be visible already (not only after the terminal event).
  await waitFor(() => {
    expect(screen.getByTestId("execution-status").textContent).toBe("运行中");
    expect(screen.getByTestId("stdout-view").textContent).toContain("head kept");
    expect(
      screen.getByText("日志超过平台保存上限，部分内容已被截断"),
    ).toBeTruthy();
  });
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
    production_worker_id: 1,
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
  expect(screen.queryByTestId("header-production-worker-warning")).toBeNull();

  window.dispatchEvent(new Event("focus"));
  await waitFor(() => {
    expect(workerCalls).toBe(2);
    expect(screen.getByTestId("worker-status").textContent).toContain("0/1 在线");
  });
  expect(screen.getByTestId("header-production-worker-warning").textContent).toContain(
    "worker-a 最近状态为离线",
  );
  expect(
    screen.getByTestId("adapter-item").querySelector(".catalog-item-attention")?.textContent,
  ).toContain("Worker 离线");

  window.dispatchEvent(new Event("focus"));
  await waitFor(() => {
    expect(workerCalls).toBe(3);
    expect(screen.getByTestId("worker-status").textContent).toContain("1/1 在线");
    expect(screen.queryByTestId("header-production-worker-warning")).toBeNull();
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
  expect(screen.getByText("测试运行")).toBeTruthy();
  expect(screen.getByText("执行记录")).toBeTruthy();
});

it("renders the two-column test run layout with input and execution panels", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [
          {
            id: 3,
            name: "only-python-worker",
            status: "online",
            last_heartbeat: "",
            capabilities: ["python"],
          },
        ],
      }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTestRunTab();
  expect(screen.getByTestId("test-input-col")).toBeTruthy();
  expect(screen.getByTestId("execution-col")).toBeTruthy();
  const context = screen.getByTestId("test-runtime-info").textContent ?? "";
  expect(context).toContain("v1");
  expect(context).toContain("Python");
  expect(context).toContain("only-python-worker（自动）");
  expect(screen.getByTestId("test-input").getAttribute("aria-label")).toBe("测试输入 JSON");
});

it("explains when multiple compatible Workers prevent automatic selection", async () => {
  const adapter = makeAdapter({
    latest_version_id: 10,
    published_version_id: 10,
    production_worker_id: null,
  });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [1, 2].map((id) => ({
          id,
          name: `worker-${id}`,
          status: "online",
          last_heartbeat: "",
          capabilities: ["python"],
        })),
      }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  expect(
    (await screen.findByTestId("header-production-worker-selection-warning")).textContent,
  ).toContain("2 个可用 Worker");
  expect(screen.queryByTestId("publish-version")).toBeNull();
  await openTestRunTab();
  const warning = screen.getByTestId("test-worker-selection-warning");
  expect(warning.textContent).toContain("2 个有效在线且兼容的 Worker");
  fireEvent.click(within(warning).getByText("打开设置"));
  await screen.findByTestId("production-worker");
});

it("guides the user to restore or register a Worker when none is available", async () => {
  const adapter = makeAdapter({
    latest_version_id: 10,
    published_version_id: 10,
    production_worker_id: null,
  });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  const headerWarning = await screen.findByTestId("header-production-worker-selection-warning");
  expect(headerWarning.textContent).toContain("当前没有有效在线且支持 Python 的 Worker");
  expect(headerWarning.textContent).toContain("恢复、启动或注册");
  await openTestRunTab();
  const testWarning = screen.getByTestId("test-worker-selection-warning");
  expect(testWarning.textContent).toContain("恢复、启动或注册");
  expect(within(testWarning).queryByText("打开设置")).toBeNull();
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

it("keeps catalog production summaries stable across adapter switches without extra requests", async () => {
  const adapterA = makeAdapter({
    id: 1,
    name: "adapter-a",
    latest_version_id: 10,
    published_version_id: 10,
    published_version_seq: 1,
  });
  const adapterB = makeAdapter({ id: 2, name: "adapter-b", latest_version_id: 20 });
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapterA, adapterB] }) },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({
        body: [{ id: 10, adapter_id: 1, seq: 1, created_at: "2026-08-11T00:00:00Z" } satisfies VersionSummary],
      }),
    },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: makeVersion() }) },
    {
      method: "GET",
      match: "/api/adapters/2/versions",
      respond: () => ({
        body: [{ id: 20, adapter_id: 2, seq: 2, created_at: "2026-08-11T00:00:00Z" } satisfies VersionSummary],
      }),
    },
    {
      method: "GET",
      match: "/api/adapters/2/versions/20",
      respond: () => ({ body: makeVersion({ id: 20, adapter_id: 2, seq: 2 }) }),
    },
  ]);

  function subOf(index: number): string {
    return screen.getAllByTestId("adapter-item")[index].querySelector(".catalog-item-sub")?.textContent ?? "";
  }

  render(<App />);
  await screen.findAllByTestId("adapter-item");

  // 未加载版本明细：列表响应直接提供真实生产 vN，不伪造 seq。
  expect(subOf(0)).toBe("Python · 待启动 v1");
  expect(subOf(1)).toBe("Python · 未发布");

  fireEvent.click(screen.getAllByTestId("adapter-item")[0]);
  await screen.findByTestId("code-editor");
  await waitFor(() => {
    expect(subOf(0)).toBe("Python · 待启动 v1");
  });

  // 切到 B：A 的已知摘要不得退化；B 展示真实的未发布状态。
  fireEvent.click(screen.getAllByTestId("adapter-item")[1]);
  await waitFor(() => {
    expect(subOf(1)).toBe("Python · 未发布");
  });
  expect(subOf(0)).toBe("Python · 待启动 v1");

  // 切回 A：缓存仍然生效。
  fireEvent.click(screen.getAllByTestId("adapter-item")[0]);
  await screen.findByTestId("code-editor");
  expect(subOf(0)).toBe("Python · 待启动 v1");

  // 不为展示 seq 增加额外请求：版本列表只在选中对应 Adapter 时加载。
  const bListCalls = fetchMock.mock.calls.filter(([url]) => String(url) === "/api/adapters/2/versions");
  expect(bListCalls).toHaveLength(1);
});

it("labels the current execution with the user-facing vN instead of the internal version id", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const pending = makeExecution({ status: "pending" });
  const succeeded = makeExecution({ status: "succeeded", duration_ms: 42 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => ({ status: 202, body: pending }),
    },
    {
      method: "GET",
      match: "/api/executions/5/events",
      respond: () => ({
        stream: `event: execution\ndata: ${JSON.stringify(succeeded)}\n\n`,
      }),
    },
    { method: "GET", match: "/api/executions/5", respond: () => ({ body: succeeded }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await openTestRunTab();
  fireEvent.click(screen.getByTestId("run-test"));

  await waitFor(() => {
    expect(screen.getByTestId("execution-status").textContent).toBe("成功");
  });
  // 主版本标识为 vN（与左栏测试版本一致）；内部 id 仅作次级调试信息。
  const versionCell = screen.getByTestId("execution-version");
  expect(versionCell.textContent).toContain("v1");
  expect(versionCell.querySelector(".execution-version-debug")?.textContent).toBe("#10");
});

// --- M3.2 production lifecycle --------------------------------------------------

it("starts production, locks the version and shows success message", async () => {
  const adapter = makeAdapter({
    latest_version_id: 10,
    published_version_id: 10,
    production_worker_id: null,
  });
  // M5.1: Start returns an Adapter (not an Execution).
  const startedAdapter = {
    ...adapter,
    production_state: "running",
    production_version_id: 10,
    production_version_seq: 1,
    production_worker_id: 3,
  };
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [
          {
            id: 3,
            name: "only-compatible-worker",
            status: "online",
            last_heartbeat: "",
            capabilities: ["python"],
          },
        ],
      }),
    },
    {
      method: "POST",
      match: "/api/adapters/1/production/start",
      respond: () => ({ status: 200, body: startedAdapter }),
    },
    {
      method: "GET",
      match: "/api/adapters/1",
      respond: () => ({
        body: {
          ...adapter,
          production_state: "running",
          production_version_id: 10,
          production_version_seq: 1,
          production_worker_id: 3,
          running_execution_id: null,
          running_version_id: null,
        },
      }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();

  expect((screen.getByTestId("start-production") as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(screen.getByTestId("start-production"));
  // M5.1: Start no longer auto-opens an Execution; shows a version lock message.
  await screen.findByText("生产入口已开启，生产版本锁定为 v1");
  expect(screen.getByTestId("production-state").textContent).toBe("生产：已启动");
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) =>
        String(url) === "/api/adapters/1/production/start" && init?.method === "POST",
    ),
  ).toBe(true);
});

it("shows a restarted entry as started and idle despite a failed previous lifecycle", async () => {
  // M5.1 regression (历史 failed → Stop → Start): Start no longer creates an
  // Execution, so running + no active Execution is the legal idle state. The
  // old abnormal derivation must not resurface, and nothing may tell the user
  // to do another Stop → Start round trip.
  const stoppedAfterFailure = makeAdapter({
    latest_version_id: 10,
    published_version_id: 10,
    published_version_seq: 1,
    production_worker_id: 3,
    production_state: "stopped",
    production_version_id: null,
    last_production_execution_id: 91,
    last_production_execution_status: "failed",
    last_production_version_id: 10,
    last_production_version_seq: 1,
  });
  const restarted = {
    ...stoppedAfterFailure,
    production_state: "running",
    production_version_id: 10,
    production_version_seq: 1,
    running_execution_id: null,
    running_version_id: null,
  };
  stubFetch([
    ...consoleWithVersionRoutes(stoppedAfterFailure, makeVersion()),
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [
          {
            id: 3,
            name: "worker-01",
            status: "online",
            last_heartbeat: "",
            capabilities: ["python"],
          },
        ],
      }),
    },
    {
      method: "POST",
      match: "/api/adapters/1/production/start",
      respond: () => ({ status: 200, body: restarted }),
    },
    {
      method: "GET",
      match: "/api/adapters/1",
      respond: () => ({ body: restarted }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();

  fireEvent.click(screen.getByTestId("start-production"));
  await screen.findByText("生产入口已开启，生产版本锁定为 v1");
  expect(screen.getByTestId("production-state").textContent).toBe("生产：已启动");
  expect(screen.getByTestId("production-execution-idle").textContent).toBe("执行：空闲");
  expect(screen.getByTestId("adapter-item").querySelector(".catalog-item-sub")?.textContent).toBe(
    "Python · 生产入口已开启 · 空闲 v1",
  );
  // The previous failure stays visible as an independent execution-result
  // notice only — never as a lifecycle alert asking for Stop → Start again.
  const failureNotice = screen.getByTestId("production-last-failure");
  expect(failureNotice.textContent).toContain("Execution #91");
  expect(failureNotice.textContent).toContain("无需 Stop → Start");
  expect(failureNotice.textContent).not.toContain("先 Stop 关闭当前生产入口");
  expect(failureNotice.textContent).not.toContain("如需恢复再 Start");
});

it("publishes v3 while v2 keeps running and explains the manual Stop then Start boundary", async () => {
  const runningV2 = makeAdapter({
    latest_version_id: 30,
    published_version_id: 20,
    published_version_seq: 2,
    production_worker_id: 3,
    production_state: "running",
    production_version_id: 20,
    production_version_seq: 2,
    running_execution_id: 77,
    running_version_id: 20,
    running_version_seq: 2,
  });
  const publishedV3 = {
    ...runningV2,
    published_version_id: 30,
    published_version_seq: 3,
  };
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [runningV2] }) },
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [
          {
            id: 3,
            name: "worker-01",
            status: "online",
            last_heartbeat: "2026-08-12T00:00:00Z",
            capabilities: ["python"],
          },
        ],
      }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({
        body: [
          { id: 30, adapter_id: 1, seq: 3, created_at: "" },
          { id: 20, adapter_id: 1, seq: 2, created_at: "" },
        ],
      }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions/30",
      respond: () => ({ body: makeVersion({ id: 30, seq: 3 }) }),
    },
    publishGateRoute(1, 30),
    {
      method: "POST",
      match: "/api/adapters/1/versions/30/publish",
      respond: () => ({ body: publishedV3 }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByTestId("publish-version"));
  await screen.findByTestId("publish-gate-ok");

  const confirmation = screen.getByTestId("publish-confirm-target").textContent ?? "";
  expect(confirmation).toContain("当前运行不会自动切换");
  expect(confirmation).toContain("人工 Stop");
  expect(confirmation).toContain("安全结束后再 Start");
  expect(confirmation).not.toContain("热切换");
  expect((screen.getByTestId("confirm-publish") as HTMLButtonElement).disabled).toBe(false);

  fireEvent.click(screen.getByTestId("confirm-publish"));
  await screen.findByTestId("published-running-mismatch");
  expect(await screen.findByText("已发布 v3；生产运行状态未自动改变")).toBeTruthy();
  expect(screen.getByTestId("running-execution").textContent).toContain("#77");
  expect(screen.getByTestId("production-state").textContent).toBe("生产：已启动");
  expect(screen.getByTestId("published-running-mismatch").textContent).toContain("已发布版本（v3）");
  expect(screen.getByTestId("published-running-mismatch").textContent).toContain("生产锁定版本（v2）");
  expect(
    screen.getByTestId("adapter-item").querySelector(".catalog-item-sub")?.textContent,
  ).toBe("Python · 生产运行 v2 · v3 待启动");
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) =>
        String(url) === "/api/adapters/1/versions/30/publish" && init?.method === "POST",
    ),
  ).toBe(true);
});

it("does not let an in-flight production refresh overwrite a completed Publish", async () => {
  PRODUCTION_REFRESH_POLICY.pollIntervalMs = 20;
  const runningV2 = makeAdapter({
    latest_version_id: 30,
    published_version_id: 20,
    published_version_seq: 2,
    production_state: "running",
    production_version_id: 20,
    production_version_seq: 2,
    running_execution_id: 77,
    running_version_id: 20,
    running_version_seq: 2,
  });
  const publishedV3 = {
    ...runningV2,
    published_version_id: 30,
    published_version_seq: 3,
  };
  let releaseStaleRefresh: () => void = () => {};
  const staleRefreshGate = new Promise<void>((resolve) => {
    releaseStaleRefresh = resolve;
  });
  let refreshCalls = 0;
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [runningV2] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({
        body: [
          { id: 30, adapter_id: 1, seq: 3, created_at: "" },
          { id: 20, adapter_id: 1, seq: 2, created_at: "" },
        ],
      }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions/30",
      respond: () => ({ body: makeVersion({ id: 30, seq: 3 }) }),
    },
    publishGateRoute(1, 30),
    {
      method: "POST",
      match: "/api/adapters/1/versions/30/publish",
      respond: () => ({ body: publishedV3 }),
    },
    {
      method: "GET",
      match: "/api/adapters/1",
      respond: async () => {
        refreshCalls += 1;
        if (refreshCalls === 1) {
          await staleRefreshGate;
          return { body: runningV2 };
        }
        return { body: publishedV3 };
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await waitFor(() => {
    expect(refreshCalls).toBe(1);
  });
  fireEvent.click(screen.getByTestId("publish-version"));
  await screen.findByTestId("publish-gate-ok");
  fireEvent.click(screen.getByTestId("confirm-publish"));
  await screen.findByTestId("published-running-mismatch");

  releaseStaleRefresh();
  await waitFor(() => {
    expect(refreshCalls).toBeGreaterThanOrEqual(2);
  });
  expect(screen.getByTestId("published-running-mismatch").textContent).toContain(
    "已发布版本（v3）",
  );
});

it("selects an explicit production Worker and keeps the retest gate visible after saving", async () => {
  const adapter = makeAdapter({
    latest_version_id: 10,
    published_version_id: 10,
    published_version_seq: 1,
    production_worker_id: 1,
  });
  const workers = [
    {
      id: 1,
      name: "worker-a",
      status: "online",
      last_heartbeat: "2026-08-12T00:00:00Z",
      capabilities: ["python"],
    },
    {
      id: 2,
      name: "worker-b",
      status: "online",
      last_heartbeat: "2026-08-12T00:00:00Z",
      capabilities: ["python"],
    },
    {
      id: 3,
      name: "worker-c",
      status: "offline",
      last_heartbeat: "2026-08-12T00:00:00Z",
      capabilities: ["python"],
    },
  ];
  let patchBody: string | null = null;
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    { method: "GET", match: "/api/workers", respond: () => ({ body: workers }) },
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: (body) => {
        patchBody = body;
        return { body: { ...adapter, production_worker_id: 2 } };
      },
    },
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => ({ status: 202, body: makeExecution({ id: 88 }) }),
    },
    {
      method: "GET",
      match: "/api/executions/88/events",
      respond: () => ({
        stream: `event: execution\ndata: ${JSON.stringify(
          makeExecution({ id: 88, worker_id: 2, status: "succeeded" }),
        )}\n\n`,
      }),
    },
    {
      method: "GET",
      match: "/api/executions/88",
      respond: () => ({ body: makeExecution({ id: 88, worker_id: 2, status: "succeeded" }) }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByTestId("adapter-settings"));
  const selector = await screen.findByTestId("production-worker");
  fireEvent.mouseDown(selector.querySelector(".ant-select-selector") ?? selector);
  expect(await screen.findAllByText("在线")).toHaveLength(2);
  await screen.findByText("离线");
  fireEvent.click(screen.getByText("worker-c"));
  expect(screen.getByTestId("production-worker-offline").textContent).toContain(
    "Worker worker-c 当前离线",
  );
  expect((screen.getByTestId("update-production-worker") as HTMLButtonElement).disabled).toBe(
    false,
  );

  fireEvent.mouseDown(selector.querySelector(".ant-select-selector") ?? selector);
  fireEvent.click(screen.getByText("worker-b"));
  expect(screen.queryByTestId("production-worker-offline")).toBeNull();

  expect(screen.getByTestId("production-worker-retest").textContent).toContain("需重新测试");
  fireEvent.click(screen.getByTestId("update-production-worker"));

  await waitFor(() => {
    expect(JSON.parse(patchBody ?? "{}")).toEqual({ production_worker_id: 2 });
  });
  // 保存后不把门禁警告当成已消除；必须真的重测。
  expect(screen.getByTestId("production-worker-retest").textContent).toContain("需重新测试");
  expect(
    screen.getByTestId("adapter-item").querySelector(".catalog-item-sub")?.textContent,
  ).not.toContain("worker-b");
  expect(screen.getByTestId("adapter-item").getAttribute("title")).toContain("Worker worker-b");

  const drawerClose = document.querySelector(".ant-drawer-close");
  if (drawerClose === null) {
    throw new Error("Adapter settings close button not found");
  }
  fireEvent.click(drawerClose);
  expect(screen.getByTestId("header-production-worker-retest").textContent).toContain(
    "建议先重新测试",
  );
  await openTestRunTab();
  fireEvent.click(screen.getByTestId("run-test"));
  await waitFor(() => {
    expect(screen.getByTestId("execution-status").textContent).toBe("成功");
  });

  fireEvent.click(screen.getByTestId("adapter-settings"));
  await screen.findByTestId("production-worker");
  expect(screen.queryByTestId("production-worker-retest")).toBeNull();
  expect(screen.queryByTestId("header-production-worker-retest")).toBeNull();
});

it("clears the retest guidance after the backend auto-selects a Worker for a successful test", async () => {
  const adapter = makeAdapter({
    latest_version_id: 10,
    published_version_id: 10,
    published_version_seq: 1,
    production_worker_id: 1,
  });
  const automaticWorker = {
    id: 1,
    name: "only-worker",
    status: "online",
    last_heartbeat: "2026-08-12T00:00:00Z",
    capabilities: ["python"],
  };
  let patchBody: string | null = null;
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    { method: "GET", match: "/api/workers", respond: () => ({ body: [automaticWorker] }) },
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: (body) => {
        patchBody = body;
        return { body: { ...adapter, production_worker_id: null } };
      },
    },
    {
      method: "POST",
      match: "/api/adapters/1/executions",
      respond: () => ({
        status: 202,
        body: makeExecution({ id: 89, worker_id: 1, status: "succeeded" }),
      }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByTestId("adapter-settings"));
  const selector = await screen.findByTestId("production-worker");
  const clear = selector.querySelector<HTMLElement>(".ant-select-clear");
  if (clear === null) {
    throw new Error("production Worker clear control not found");
  }
  fireEvent.mouseDown(clear);
  fireEvent.click(screen.getByTestId("update-production-worker"));
  await waitFor(() => {
    expect(JSON.parse(patchBody ?? "{}")).toEqual({ production_worker_id: null });
  });
  expect(screen.getByTestId("production-worker-retest").textContent).toContain("需重新测试");

  const drawerClose = document.querySelector(".ant-drawer-close");
  if (drawerClose === null) {
    throw new Error("Adapter settings close button not found");
  }
  fireEvent.click(drawerClose);
  await openTestRunTab();
  expect(screen.getByTestId("test-runtime-info").textContent).toContain("only-worker（自动）");
  fireEvent.click(screen.getByTestId("run-test"));
  await waitFor(() => {
    expect(screen.getByTestId("execution-status").textContent).toBe("成功");
  });

  fireEvent.click(screen.getByTestId("adapter-settings"));
  await screen.findByTestId("production-worker");
  expect(screen.queryByTestId("production-worker-retest")).toBeNull();
  expect(screen.queryByTestId("header-production-worker-retest")).toBeNull();
});

it("refreshes a naturally completed production run into the started and idle state", async () => {
  PRODUCTION_REFRESH_POLICY.pollIntervalMs = 20;
  const running = makeAdapter({
    latest_version_id: 10,
    published_version_id: 10,
    published_version_seq: 1,
    production_worker_id: 3,
    production_state: "running",
    running_execution_id: 77,
    running_version_id: 10,
    running_version_seq: 1,
  });
  const idle = {
    ...running,
    running_execution_id: null,
    running_version_id: null,
    running_version_seq: null,
    last_production_execution_id: 77,
    last_production_execution_status: "succeeded" as const,
    last_production_version_id: 10,
    last_production_version_seq: 1,
  };
  stubFetch([
    ...consoleWithVersionRoutes(running, makeVersion()),
    { method: "GET", match: "/api/adapters/1", respond: () => ({ body: idle }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await screen.findByTestId("production-execution-idle");
  expect(screen.getByTestId("production-state").textContent).toBe("生产：已启动");
  expect(screen.getByTestId("production-execution-idle").textContent).toBe("执行：空闲");
  // 生产入口仍已启动；必须人工 Stop，不得直接再 Start。
  expect(screen.queryByTestId("start-production")).toBeNull();
  expect(screen.getByTestId("stop-production")).toBeTruthy();
});

it("shows an unvisited started-and-idle Adapter with the server-derived production version seq", async () => {
  // M5.1: Start succeeded, no active Execution, and the row is never visited,
  // so no local version list exists: the vN label must come from the
  // server-provided production_version_id/production_version_seq alone.
  const idle = makeAdapter({
    latest_version_id: 20,
    published_version_id: 20,
    published_version_seq: 2,
    production_worker_id: 3,
    production_state: "running",
    production_version_id: 10,
    production_version_seq: 1,
    running_execution_id: null,
    running_version_id: null,
    running_version_seq: null,
    last_production_execution_id: 77,
    last_production_execution_status: "succeeded",
    last_production_version_id: 10,
    last_production_version_seq: 1,
  });
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [idle] }) },
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [
          {
            id: 3,
            name: "worker-01",
            status: "online",
            last_heartbeat: "2026-08-12T00:00:00Z",
            capabilities: ["python"],
          },
        ],
      }),
    },
  ]);

  render(<App />);
  const [row] = await screen.findAllByTestId("adapter-item");
  expect(row.querySelector(".catalog-item-sub")?.textContent).toBe(
    "Python · 生产入口已开启 · 空闲 v1 · v2 待启动",
  );
});

it("keeps lifecycle actions locked during Stop(wait) until the active execution is terminal", async () => {
  const running = makeAdapter({
    latest_version_id: 10,
    published_version_id: 10,
    published_version_seq: 1,
    production_state: "running",
    running_execution_id: 77,
    running_version_id: 10,
    running_version_seq: 1,
  });
  const stopping = { ...running, production_state: "stopped" as const };
  const stopped = {
    ...stopping,
    running_execution_id: null,
    running_version_id: null,
    running_version_seq: null,
    last_production_execution_id: 77,
    last_production_execution_status: "succeeded" as const,
    last_production_version_id: 10,
  };
  let getCalls = 0;
  stubFetch([
    ...consoleWithVersionRoutes(running, makeVersion()),
    {
      method: "POST",
      match: "/api/adapters/1/production/stop",
      respond: () => ({ body: stopping }),
    },
    {
      method: "GET",
      match: "/api/adapters/1",
      respond: () => {
        getCalls += 1;
        return { body: stopped };
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  PRODUCTION_REFRESH_POLICY.pollIntervalMs = 100;
  fireEvent.click(screen.getByTestId("stop-production"));
  fireEvent.click(await screen.findByTestId("stop-mode-wait"));

  await screen.findByTestId("production-stopping");
  const startWhileStopping = screen.getByTestId("start-production") as HTMLButtonElement;
  expect(startWhileStopping.disabled).toBe(true);
  expect(startWhileStopping.closest(".action-with-reason")?.getAttribute("title")).toContain(
    "等待 Execution #77 完成",
  );
  expect(screen.getAllByText("生产入口已关闭，等待 Execution #77 完成")).toHaveLength(1);
  expect(screen.getByTestId("production-stopping").textContent).toContain("Execution #77");
  fireEvent.click(screen.getByTestId("adapter-settings"));
  expect((screen.getByTestId("unpublish-adapter") as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByTestId("archive-adapter") as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByTestId("settings-production-stopping").textContent).toContain("Execution #77");

  await waitFor(
    () => {
      expect(screen.getByTestId("production-state").textContent).toBe("生产：已停止");
    },
    { timeout: 3000 },
  );
  expect(getCalls).toBeGreaterThan(0);
  expect((screen.getByTestId("start-production") as HTMLButtonElement).disabled).toBe(false);
  expect((screen.getByTestId("unpublish-adapter") as HTMLButtonElement).disabled).toBe(false);
  expect((screen.getByTestId("archive-adapter") as HTMLButtonElement).disabled).toBe(false);
});

it("labels a stopped Catalog row with the last run instead of claiming it is running", async () => {
  const stopped = makeAdapter({
    latest_version_id: 20,
    published_version_id: 20,
    published_version_seq: 2,
    production_worker_id: 3,
    production_state: "stopped",
    running_execution_id: null,
    running_version_id: null,
    running_version_seq: null,
    last_production_execution_id: 77,
    last_production_execution_status: "succeeded",
    last_production_version_id: 10,
    last_production_version_seq: 1,
  });
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [stopped] }) },
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [
          {
            id: 3,
            name: "worker-01",
            status: "online",
            last_heartbeat: "2026-08-12T00:00:00Z",
            capabilities: ["python"],
          },
        ],
      }),
    },
  ]);

  render(<App />);
  const [row] = await screen.findAllByTestId("adapter-item");
  const subtitle = row.querySelector(".catalog-item-sub")?.textContent ?? "";
  expect(subtitle).toBe("Python · 已停止 · 上次 v1 · v2 待启动");
  expect(subtitle).not.toContain("· 运行 v1");
});

it("blocks the publish confirmation when the gate rejects the version", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, production_worker_id: 3 });
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    publishGateRoute(1, 10, {
      allowed: false,
      reason: "not_tested_on_production_worker",
      last_test: null,
    }),
    {
      method: "POST",
      match: "/api/adapters/1/versions/10/publish",
      respond: () => {
        throw new Error("publish must not be issued when the gate rejects");
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();

  fireEvent.click(screen.getByTestId("publish-version"));
  await screen.findByTestId("publish-gate-blocked");
  expect(screen.getByTestId("publish-gate-blocked").textContent).toContain("尚未在生产 Worker 上测试");
  expect((screen.getByTestId("confirm-publish") as HTMLButtonElement).disabled).toBe(true);
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url).endsWith("/publish") && init?.method === "POST",
    ),
  ).toBe(false);
});

it("stops production with terminate via the stop modal", async () => {
  const adapter = makeAdapter({
    latest_version_id: 10,
    published_version_id: 10,
    production_state: "running",
    running_execution_id: 77,
    running_version_id: 10,
  });
  let stopBody: string | null = null;
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "POST",
      match: "/api/adapters/1/production/stop",
      respond: (body) => {
        stopBody = body;
        return {
          body: { ...adapter, production_state: "stopped", running_execution_id: null, running_version_id: null },
        };
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  expect(screen.getByTestId("production-state").textContent).toBe("生产：已启动");

  fireEvent.click(screen.getByTestId("stop-production"));
  fireEvent.click(await screen.findByTestId("stop-mode-terminate"));

  await waitFor(() => {
    expect(screen.getByTestId("production-state").textContent).toBe("生产：已停止");
  });
  expect(JSON.parse(stopBody ?? "")).toEqual({ mode: "terminate" });
  // 停止后运行中 Execution 标签消失，启动按钮重新可用。
  expect(screen.queryByTestId("running-execution")).toBeNull();
  expect(screen.getByTestId("start-production")).toBeTruthy();
});

it("hides archived adapters from the active catalog and disables editing when archived", async () => {
  const archived = makeAdapter({ archived_at: "2026-08-11T01:00:00Z" });
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [archived] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [] }) },
  ]);

  render(<App />);
  // 活跃视图默认不展示已归档 Adapter。
  await waitFor(() => {
    expect(screen.queryByTestId("adapter-item")).toBeNull();
  });

  // 切到已归档视图后可见并可选中。
  fireEvent.click(screen.getByText("已归档"));
  fireEvent.click(await screen.findByTestId("adapter-item"));
  await screen.findByTestId("archived-notice");
  const saveButton = screen.getByTestId("save-version") as HTMLButtonElement;
  const publishButton = screen.getByTestId("publish-version") as HTMLButtonElement;
  expect(saveButton.disabled).toBe(true);
  expect(publishButton.disabled).toBe(true);
  expect(saveButton.closest(".action-with-reason")?.getAttribute("title")).toContain(
    "请先在设置中恢复",
  );
  expect(publishButton.closest(".action-with-reason")?.getAttribute("aria-label")).toContain(
    "发布不可用",
  );
  expect(screen.queryByTestId("start-production")).toBeNull();
});

// --- M3.2 配置区 / 系统设置 / Diff -------------------------------------------

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

it("shows a Java publish diff with the matching Monaco mode and dependency label", async () => {
  const adapter = makeAdapter({
    language: "java",
    latest_version_id: 10,
    published_version_id: 11,
  });
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    {
      method: "GET",
      match: "/api/adapters/1/versions",
      respond: () => ({
        body: [
          { id: 10, adapter_id: 1, seq: 2, created_at: "" },
          { id: 11, adapter_id: 1, seq: 1, created_at: "" },
        ],
      }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions/10",
      respond: () => ({ body: makeVersion({ id: 10, seq: 2, code: "target-code\n" }) }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/versions/11",
      respond: () => ({ body: makeVersion({ id: 11, seq: 1, code: "running-code\n" }) }),
    },
    publishGateRoute(1, 10),
    {
      method: "GET",
      match: "/api/adapters/1/credential-bindings",
      respond: () => ({ body: [] }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();

  fireEvent.click(screen.getByTestId("publish-version"));
  await screen.findByTestId("publish-gate-ok");
  fireEvent.click(screen.getByTestId("publish-diff"));

  await screen.findByTestId("version-diff");
  // 默认展示代码窗格：original 为当前生产版本，modified 为发布目标。
  const diff = screen.getByTestId("diff-editor");
  expect(diff.getAttribute("data-monaco-language")).toBe("java");
  expect(diff.getAttribute("data-original")).toBe("running-code\n");
  expect(diff.getAttribute("data-modified")).toBe("target-code\n");
  expect(within(screen.getByTestId("version-diff")).getByText("Maven 依赖")).toBeTruthy();
  // Diff 覆盖绑定引用：需拉取当前 Adapter 绑定。
  expect(
    fetchMock.mock.calls.some(([url]) => String(url) === "/api/adapters/1/credential-bindings"),
  ).toBe(true);
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

it("keeps archived Adapter editors read-only and disables Candidate Apply", async () => {
  const archived = makeAdapter({ archived_at: "2026-08-11T01:00:00Z" });
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
    "请先在设置中恢复",
  );
});

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

// --- Schedule Trigger (M5.2) ---------------------------------------------------

interface ScheduleBody {
  adapter_id: number;
  enabled: boolean;
  cron: string;
  timezone: string;
  input: unknown;
  next_run_at: string | null;
  updated_at: string;
}

function makeSchedule(overrides: Partial<ScheduleBody> = {}): ScheduleBody {
  return {
    adapter_id: 1,
    enabled: true,
    cron: "0 */2 * * *",
    timezone: "Asia/Shanghai",
    input: { full_sync: true },
    next_run_at: "2026-08-13T10:00:00Z",
    updated_at: "2026-08-13T00:00:00Z",
    ...overrides,
  };
}

const scheduleNotConfiguredRoute: Route = {
  method: "GET",
  match: "/api/adapters/1/schedule",
  respond: () => ({
    status: 404,
    body: { detail: { code: "schedule_not_configured", message: "Schedule is not configured" } },
  }),
};

const webhookNotConfiguredRoute: Route = {
  method: "GET",
  match: "/api/adapters/1/webhook",
  respond: () => ({
    status: 404,
    body: { detail: { code: "webhook_not_configured", message: "Webhook is not configured" } },
  }),
};

// M5.3：触发器 Tab 中 WebhookTriggerPanel 与 Schedule 并列渲染，打开该 Tab 会额外
// 请求 Webhook 配置与凭据列表；Schedule 测试补上这两条路由，让 Webhook 区域保持
// 「未配置 + 无凭据」的初始态，不影响既有断言。
const webhookSideRoutes: Route[] = [
  webhookNotConfiguredRoute,
  { method: "GET", match: "/api/credentials", respond: () => ({ body: [] }) },
];

async function openTriggerTab() {
  fireEvent.click(screen.getByText("触发器"));
  await screen.findByTestId("schedule-cron");
}

it("shows the trigger tab with an empty schedule form before configuration", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    ...webhookSideRoutes,
    scheduleNotConfiguredRoute,
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  expect(valueOf("schedule-cron")).toBe("0 */2 * * *");
  expect((screen.getByTestId("schedule-enabled") as HTMLElement).getAttribute("aria-checked")).toBe("false");
  expect(screen.getByTestId("schedule-next-run").textContent).toBe("已禁用，不计划执行");
  expect(screen.queryByTestId("schedule-production-closed-hint")).toBeNull();
  expect(screen.queryByTestId("schedule-error")).toBeNull();
});

it("loads an enabled schedule and warns while the production entry is closed", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, production_state: "stopped" });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    ...webhookSideRoutes,
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({ body: makeSchedule() }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  expect(valueOf("schedule-cron")).toBe("0 */2 * * *");
  expect(valueOf("schedule-timezone")).toBe("Asia/Shanghai");
  expect(valueOf("schedule-input")).toContain("full_sync");
  expect((screen.getByTestId("schedule-enabled") as HTMLElement).getAttribute("aria-checked")).toBe("true");
  expect(screen.getByTestId("schedule-next-run").textContent).not.toBe("已禁用，不计划执行");
  expect(screen.getByTestId("schedule-production-closed-hint").textContent).toContain(
    "生产入口关闭期间不会执行",
  );
});

it("hides the production-closed hint while production is running", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, production_state: "running" });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    ...webhookSideRoutes,
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({ body: makeSchedule() }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  await screen.findByTestId("schedule-next-run");
  expect(screen.queryByTestId("schedule-production-closed-hint")).toBeNull();
});

it("saves the schedule and re-displays the normalized server response", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, production_state: "running" });
  let putBody = "";
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    ...webhookSideRoutes,
    scheduleNotConfiguredRoute,
    {
      method: "PUT",
      match: "/api/adapters/1/schedule",
      respond: (body) => {
        putBody = body ?? "";
        return {
          body: makeSchedule({
            enabled: true,
            cron: "0 9 * * *",
            timezone: "UTC",
            input: { mode: "full" },
            next_run_at: "2026-08-14T09:00:00Z",
          }),
        };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  fireEvent.click(screen.getByTestId("schedule-enabled"));
  fireEvent.change(screen.getByTestId("schedule-cron"), { target: { value: "  0  9  *  *  * " } });
  fireEvent.change(screen.getByTestId("schedule-timezone"), { target: { value: "UTC" } });
  fireEvent.change(screen.getByTestId("schedule-input"), { target: { value: '{"mode":"full"}' } });
  fireEvent.click(screen.getByTestId("schedule-save"));

  await screen.findByTestId("schedule-notice");
  expect(JSON.parse(putBody)).toEqual({
    enabled: true,
    cron: "  0  9  *  *  * ",
    timezone: "UTC",
    input: { mode: "full" },
  });
  // The server-normalized cron and cursor are re-displayed after saving.
  expect(valueOf("schedule-cron")).toBe("0 9 * * *");
  expect(screen.getByTestId("schedule-next-run").textContent).not.toBe("已禁用，不计划执行");
  expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/adapters/1/schedule")).toBe(true);
});

it("shows the cron validation error and persists nothing", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    ...webhookSideRoutes,
    scheduleNotConfiguredRoute,
    {
      method: "PUT",
      match: "/api/adapters/1/schedule",
      respond: () => ({
        status: 422,
        body: { detail: { code: "schedule_invalid_cron", message: "cron must have exactly 5 fields" } },
      }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  fireEvent.change(screen.getByTestId("schedule-cron"), { target: { value: "not-a-cron" } });
  fireEvent.click(screen.getByTestId("schedule-save"));

  const error = await screen.findByTestId("schedule-error");
  expect(error.textContent).toContain("Cron 表达式无效");
  expect(screen.queryByTestId("schedule-notice")).toBeNull();
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/schedule" && init?.method === "PUT",
    ),
  ).toBe(true);
});

it("shows the timezone validation error", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    ...webhookSideRoutes,
    scheduleNotConfiguredRoute,
    {
      method: "PUT",
      match: "/api/adapters/1/schedule",
      respond: () => ({
        status: 422,
        body: {
          detail: { code: "schedule_invalid_timezone", message: "timezone is not a valid IANA timezone" },
        },
      }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  fireEvent.change(screen.getByTestId("schedule-timezone"), { target: { value: "Not/AZone" } });
  fireEvent.click(screen.getByTestId("schedule-save"));

  const error = await screen.findByTestId("schedule-error");
  expect(error.textContent).toContain("时区无效");
});

it("rejects an unparsable input locally without calling the API", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    ...webhookSideRoutes,
    scheduleNotConfiguredRoute,
    {
      method: "PUT",
      match: "/api/adapters/1/schedule",
      respond: () => ({ body: makeSchedule() }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  fireEvent.change(screen.getByTestId("schedule-input"), { target: { value: "{broken" } });
  fireEvent.click(screen.getByTestId("schedule-save"));

  const error = await screen.findByTestId("schedule-error");
  expect(error.textContent).toContain("Input 必须是合法 JSON");
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/schedule" && init?.method === "PUT",
    ),
  ).toBe(false);
});

it("renders the schedule read-only on an archived adapter and never calls PUT", async () => {
  const adapter = makeAdapter({
    latest_version_id: 10,
    archived_at: "2026-08-13T00:00:00Z",
    production_state: "stopped",
  });
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    ...webhookSideRoutes,
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({ body: makeSchedule() }),
    },
  ]);
  render(<App />);
  await screen.findByTestId("adapter-catalog");
  // 已归档 Adapter 只在归档视图中可见。
  fireEvent.click(screen.getByText("已归档"));
  await selectFirstAdapter();
  await openTriggerTab();

  // GET 仍可查看配置，但编辑与保存全部禁用并给出只读提示。
  expect(screen.getByTestId("schedule-archived-hint").textContent).toContain("只读");
  expect(valueOf("schedule-cron")).toBe("0 */2 * * *");
  expect((screen.getByTestId("schedule-cron") as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByTestId("schedule-timezone") as HTMLInputElement).disabled).toBe(true);
  expect((screen.getByTestId("schedule-input") as HTMLTextAreaElement).disabled).toBe(true);
  expect((screen.getByTestId("schedule-save") as HTMLButtonElement).disabled).toBe(true);

  fireEvent.click(screen.getByTestId("schedule-save"));
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/schedule" && init?.method === "PUT",
    ),
  ).toBe(false);
});

it("reloads the stale next_run_at through the refresh button", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, production_state: "running" });
  let scheduleGets = 0;
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    ...webhookSideRoutes,
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => {
        scheduleGets += 1;
        // 调度器会推进游标：第二次 GET 返回更晚的计划点。
        return {
          body: makeSchedule({
            next_run_at: scheduleGets === 1 ? "2026-08-13T10:00:00Z" : "2026-08-13T12:00:00Z",
          }),
        };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  const before = screen.getByTestId("schedule-next-run").textContent;
  expect(before).not.toBe("已禁用，不计划执行");

  fireEvent.click(screen.getByTestId("schedule-refresh"));
  await waitFor(() => {
    expect(screen.getByTestId("schedule-next-run").textContent).not.toBe(before);
  });
  expect(scheduleGets).toBe(2);
  expect(
    fetchMock.mock.calls.filter(([url]) => String(url) === "/api/adapters/1/schedule"),
  ).toHaveLength(2);
});

it("shows scheduled trigger rows and the planned time in history", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, production_state: "running" });
  const summary = makeSummary({
    trigger: "schedule",
    scheduled_for: "2026-08-13T10:00:00Z",
    worker_id: 3,
    worker_name: "prod-worker",
  });
  const detail = makeExecution({
    id: 6,
    trigger: "schedule",
    scheduled_for: "2026-08-13T10:00:00Z",
    worker_id: 3,
    status: "succeeded",
  });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: /\/api\/adapters\/1\/executions\?/,
      respond: () => ({ body: { items: [summary], next_before_id: null } }),
    },
    { method: "GET", match: "/api/executions/6", respond: () => ({ body: detail }) },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByText("执行记录"));

  const [row] = await screen.findAllByTestId("history-row");
  expect(row.textContent).toContain("定时触发");
  expect(screen.getByTestId("history-scheduled-for").textContent).toContain("计划");

  fireEvent.click(row);
  const drawer = await screen.findByText("Execution #6");
  expect(drawer).toBeTruthy();
  await waitFor(() => {
    const drawerBody = document.querySelector(".ant-drawer-content");
    if (!(drawerBody instanceof HTMLElement)) {
      throw new Error("Execution detail drawer not found");
    }
    expect(within(drawerBody).getByText("定时触发")).toBeTruthy();
    expect(within(drawerBody).getByText("计划时间")).toBeTruthy();
  });
});

// --- Webhook Trigger (M5.3) ---------------------------------------------------

interface WebhookBody {
  adapter_id: number;
  enabled: boolean;
  public_id: string;
  hook_path: string;
  credential_id: number;
  credential_name: string;
  created_at: string;
  updated_at: string;
}

function makeWebhook(overrides: Partial<WebhookBody> = {}): WebhookBody {
  return {
    adapter_id: 1,
    enabled: true,
    public_id: "AbC123_unguessable",
    hook_path: "/api/hooks/AbC123_unguessable",
    credential_id: 7,
    credential_name: "hook-token",
    created_at: "2026-08-13T00:00:00Z",
    updated_at: "2026-08-13T00:00:00Z",
    ...overrides,
  };
}

function credentialsRoute(credentials: Array<{ id: number; name: string; type: string }>): Route {
  return {
    method: "GET",
    match: "/api/credentials",
    respond: () => ({
      body: credentials.map((credential) => ({ ...credential, created_at: "", updated_at: "" })),
    }),
  };
}

/** 在 Webhook 区域的凭据下拉中选中指定凭据。 */
async function selectWebhookCredential(name: string) {
  const selector = screen.getByTestId("webhook-credential");
  fireEvent.mouseDown(selector.querySelector(".ant-select-selector") ?? selector);
  fireEvent.click(await screen.findByText(name));
}

it("shows an empty webhook form before configuration and lists only token credentials", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, production_state: "running" });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    scheduleNotConfiguredRoute,
    credentialsRoute([
      { id: 7, name: "hook-token", type: "token" },
      { id: 8, name: "db-password", type: "password" },
    ]),
    webhookNotConfiguredRoute,
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  await screen.findByTestId("webhook-enabled");
  // 未配置是合法初始态：不展示加载错误，也不展示地址与示例。
  expect(screen.queryByTestId("webhook-load-error")).toBeNull();
  expect(screen.queryByTestId("webhook-no-token-credential")).toBeNull();
  expect((screen.getByTestId("webhook-enabled") as HTMLElement).getAttribute("aria-checked")).toBe("false");
  expect(screen.queryByTestId("webhook-url")).toBeNull();
  expect((screen.getByTestId("webhook-save") as HTMLButtonElement).disabled).toBe(true);

  // 凭据下拉仅列出 token 类型：password 凭据不出现。
  const selector = screen.getByTestId("webhook-credential");
  fireEvent.mouseDown(selector.querySelector(".ant-select-selector") ?? selector);
  expect(await screen.findByText("hook-token")).toBeTruthy();
  expect(screen.queryByText("db-password")).toBeNull();
});

it("warns when there is no token credential and keeps save disabled", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    scheduleNotConfiguredRoute,
    credentialsRoute([{ id: 8, name: "db-password", type: "password" }]),
    webhookNotConfiguredRoute,
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  await screen.findByTestId("webhook-enabled");
  expect(screen.getByTestId("webhook-no-token-credential").textContent).toContain("token");
  expect((screen.getByTestId("webhook-save") as HTMLButtonElement).disabled).toBe(true);
});

it("shows the credential load failure instead of pretending there is no token credential", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    scheduleNotConfiguredRoute,
    webhookNotConfiguredRoute,
    {
      method: "GET",
      match: "/api/credentials",
      respond: () => ({
        status: 503,
        body: {
          detail: { code: "secret_store_unavailable", message: "Secret Store is unavailable" },
        },
      }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  // 凭据列表加载失败必须明确报错，而不是落入「尚无 token 凭据」的空状态。
  const error = await screen.findByTestId("webhook-load-error");
  expect(error.textContent).toContain("Secret Store is unavailable");
  expect(error.textContent).toContain("secret_store_unavailable");
  expect(screen.queryByTestId("webhook-no-token-credential")).toBeNull();
  expect(screen.queryByTestId("webhook-enabled")).toBeNull();
});

it("saves the webhook and shows the stable URL and example request without the secret", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, production_state: "running" });
  let putBody = "";
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    scheduleNotConfiguredRoute,
    credentialsRoute([{ id: 7, name: "hook-token", type: "token" }]),
    webhookNotConfiguredRoute,
    {
      method: "PUT",
      match: "/api/adapters/1/webhook",
      respond: (body) => {
        putBody = body ?? "";
        return { body: makeWebhook() };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  await screen.findByTestId("webhook-enabled");
  fireEvent.click(screen.getByTestId("webhook-enabled"));
  await selectWebhookCredential("hook-token");
  fireEvent.click(screen.getByTestId("webhook-save"));

  await screen.findByTestId("webhook-notice");
  expect(JSON.parse(putBody)).toEqual({ enabled: true, credential_id: 7 });
  // 服务端返回的地址保持稳定展示，生产运行中不展示关闭提示。
  expect(valueOf("webhook-url")).toBe(window.location.origin + "/api/hooks/AbC123_unguessable");
  expect(screen.queryByTestId("webhook-production-closed-hint")).toBeNull();
  const example = screen.getByTestId("webhook-example").textContent ?? "";
  expect(example).toContain("POST /api/hooks/AbC123_unguessable");
  expect(example).toContain("Bearer <token>");
  // 示例中只出现占位符，不出现任何 token 真值。
  expect(example).not.toContain("s3cret-token-value");
});

it("loads a configured webhook, can disable it, and warns while production is closed", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, production_state: "stopped" });
  let putBody = "";
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    scheduleNotConfiguredRoute,
    credentialsRoute([{ id: 7, name: "hook-token", type: "token" }]),
    {
      method: "GET",
      match: "/api/adapters/1/webhook",
      respond: () => ({ body: makeWebhook() }),
    },
    {
      method: "PUT",
      match: "/api/adapters/1/webhook",
      respond: (body) => {
        putBody = body ?? "";
        return { body: makeWebhook({ enabled: false }) };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  await screen.findByTestId("webhook-url");
  expect((screen.getByTestId("webhook-enabled") as HTMLElement).getAttribute("aria-checked")).toBe("true");
  expect(screen.getByTestId("webhook-production-closed-hint").textContent).toContain(
    "生产入口当前关闭",
  );

  fireEvent.click(screen.getByTestId("webhook-enabled"));
  fireEvent.click(screen.getByTestId("webhook-save"));
  await screen.findByTestId("webhook-notice");
  expect(JSON.parse(putBody)).toEqual({ enabled: false, credential_id: 7 });
});

it("copies the webhook URL to the clipboard", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  const adapter = makeAdapter({ latest_version_id: 10, production_state: "running" });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    scheduleNotConfiguredRoute,
    credentialsRoute([{ id: 7, name: "hook-token", type: "token" }]),
    {
      method: "GET",
      match: "/api/adapters/1/webhook",
      respond: () => ({ body: makeWebhook() }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  await screen.findByTestId("webhook-url");
  fireEvent.click(screen.getByTestId("webhook-copy"));
  await screen.findByTestId("webhook-notice");
  expect(writeText).toHaveBeenCalledWith(window.location.origin + "/api/hooks/AbC123_unguessable");
  expect(screen.getByTestId("webhook-notice").textContent).toContain("已复制");
});

it("shows a copy fallback hint when the clipboard is unavailable", async () => {
  const writeText = vi.fn().mockRejectedValue(new Error("clipboard denied"));
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  const adapter = makeAdapter({ latest_version_id: 10, production_state: "running" });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    scheduleNotConfiguredRoute,
    credentialsRoute([{ id: 7, name: "hook-token", type: "token" }]),
    {
      method: "GET",
      match: "/api/adapters/1/webhook",
      respond: () => ({ body: makeWebhook() }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  await screen.findByTestId("webhook-url");
  fireEvent.click(screen.getByTestId("webhook-copy"));
  const fallback = await screen.findByTestId("webhook-copy-error");
  expect(fallback.textContent).toContain("手动");
});

it("maps the server rejection to a stable Chinese message", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    scheduleNotConfiguredRoute,
    credentialsRoute([{ id: 7, name: "hook-token", type: "token" }]),
    webhookNotConfiguredRoute,
    {
      method: "PUT",
      match: "/api/adapters/1/webhook",
      respond: () => ({
        status: 422,
        body: {
          detail: {
            code: "webhook_credential_type_invalid",
            message: "credential must be token type",
          },
        },
      }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openTriggerTab();

  await screen.findByTestId("webhook-enabled");
  await selectWebhookCredential("hook-token");
  fireEvent.click(screen.getByTestId("webhook-save"));

  const error = await screen.findByTestId("webhook-error");
  expect(error.textContent).toContain("只能绑定 token 类型的凭据");
  expect(screen.queryByTestId("webhook-notice")).toBeNull();
});

it("renders the webhook read-only on an archived adapter and never calls PUT", async () => {
  const adapter = makeAdapter({
    latest_version_id: 10,
    archived_at: "2026-08-13T00:00:00Z",
    production_state: "stopped",
  });
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    // 已配置的 Webhook 路由需列在 webhookSideRoutes 的 404 路由之前（首个匹配生效）。
    {
      method: "GET",
      match: "/api/adapters/1/webhook",
      respond: () => ({ body: makeWebhook() }),
    },
    ...webhookSideRoutes,
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({ body: makeSchedule() }),
    },
  ]);
  render(<App />);
  await screen.findByTestId("adapter-catalog");
  // 已归档 Adapter 只在归档视图中可见。
  fireEvent.click(screen.getByText("已归档"));
  await selectFirstAdapter();
  await openTriggerTab();

  // GET 仍可查看配置，但编辑与保存全部禁用并给出只读提示。
  await screen.findByTestId("webhook-url");
  expect(screen.getByTestId("webhook-archived-hint").textContent).toContain("只读");
  expect(valueOf("webhook-url")).toBe(window.location.origin + "/api/hooks/AbC123_unguessable");
  expect((screen.getByTestId("webhook-enabled") as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByTestId("webhook-save") as HTMLButtonElement).disabled).toBe(true);

  fireEvent.click(screen.getByTestId("webhook-save"));
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT",
    ),
  ).toBe(false);
});

it("shows webhook trigger rows in execution history", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, production_state: "running" });
  const summary = makeSummary({ trigger: "webhook", worker_id: 3, worker_name: "prod-worker" });
  const detail = makeExecution({
    id: 6,
    trigger: "webhook",
    worker_id: 3,
    status: "succeeded",
  });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: /\/api\/adapters\/1\/executions\?/,
      respond: () => ({ body: { items: [summary], next_before_id: null } }),
    },
    { method: "GET", match: "/api/executions/6", respond: () => ({ body: detail }) },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByText("执行记录"));

  const [row] = await screen.findAllByTestId("history-row");
  expect(row.textContent).toContain("Webhook");

  fireEvent.click(row);
  await screen.findByText("Execution #6");
  await waitFor(() => {
    const drawerBody = document.querySelector(".ant-drawer-content");
    if (!(drawerBody instanceof HTMLElement)) {
      throw new Error("Execution detail drawer not found");
    }
    expect(within(drawerBody).getByText("Webhook")).toBeTruthy();
  });
});
