import { readFileSync } from "node:fs";
import { join } from "node:path";

import { createRef, useEffect } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import App, {
  EDITOR_THEME_STORAGE_KEY,
  TOKEN_STORAGE_KEY,
} from "./App";
import { setAuthToken } from "./api";
import TaskRunSettingsPanel from "./components/TaskRunSettingsPanel";
import type { TaskRunSettingsHandle } from "./components/TaskRunSettingsPanel";
import TaskWorkbenchHeader from "./components/TaskWorkbenchHeader";
import WebhookWorkbenchHeader from "./components/WebhookWorkbenchHeader";
import { FALLBACK_POLICY } from "./fallback-policy";
import { RUNTIME_REFRESH_POLICY } from "./runtime-refresh-policy";
import { WORKER_REFRESH_POLICY } from "./worker-refresh-policy";
import type {
  Adapter,
  AiAssistResponse,
  AiAttachmentCapabilities,
  AiCandidate,
  Execution,
  ExecutionSummary,
  VersionDetail,
  VersionSummary,
} from "./types";

// The Monaco editor is replaced by a plain textarea so tests exercise the DLR
// business integration (value / change / save) instead of the editor itself.
// readOnly is honored so the mutation-time interaction lock is testable; the
// theme prop is mirrored so theme switching stays assertable. The onMount
// selection harness lets M5.5.5 tests drive Monaco-style selections.
const { monacoHarness } = vi.hoisted(() => {
  interface FakeSelection {
    startLineNumber: number;
    startColumn: number;
    endLineNumber: number;
    endColumn: number;
  }
  const state: { selection: FakeSelection | null; text: string } = {
    selection: null,
    text: "",
  };
  const listeners = new Set<() => void>();
  return {
    monacoHarness: {
      setSelection(selection: FakeSelection | null) {
        state.selection = selection;
        listeners.forEach((listener) => listener());
      },
      getSelection: (): FakeSelection | null => state.selection,
      setText(text: string) {
        state.text = text;
      },
      getText: (): string => state.text,
      subscribe(listener: () => void): () => void {
        listeners.add(listener);
        return () => {
          listeners.delete(listener);
        };
      },
      reset() {
        state.selection = null;
        state.text = "";
        listeners.clear();
      },
    },
  };
});

vi.mock("@monaco-editor/react", () => ({
  default: function Editor(props: {
    value?: string;
    onChange?: (value: string | undefined) => void;
    options?: { readOnly?: boolean };
    theme?: string;
    language?: string;
    onMount?: (editor: unknown, monaco: unknown) => void;
  }) {
    // Register the fake editor once per mount, mirroring real Monaco.
    useEffect(() => {
      const fakeEditor = {
        getSelection: () => {
          const selection = monacoHarness.getSelection();
          if (selection === null) {
            return null;
          }
          return {
            ...selection,
            isEmpty: () =>
              selection.startLineNumber === selection.endLineNumber &&
              selection.startColumn === selection.endColumn,
          };
        },
        getModel: () => ({
          getValueInRange: () => monacoHarness.getText(),
        }),
        onDidChangeCursorSelection: (listener: () => void) =>
          monacoHarness.subscribe(listener),
      };
      props.onMount?.(fakeEditor, {});
      // eslint-disable-next-line react-hooks/exhaustive-deps -- mock: mount-once semantics
    }, []);
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
    theme?: string;
  }) {
    return (
      <div
        data-testid="diff-editor"
        data-monaco-theme={props.theme ?? ""}
        data-monaco-language={props.language ?? ""}
        data-original={props.original ?? ""}
        data-modified={props.modified ?? ""}
      />
    );
  },
  // M5.5.9：App 通过 loader.init() 兜底重设全局 Monaco 主题。
  loader: {
    init: () =>
      Promise.resolve({
        editor: { setTheme: () => undefined },
      }),
  },
}));

const STARTER_CODE = "def handle(context, input):\n    return input\n";

const TASK_STARTER_CODE =
  "def handle(context, input):\n" +
  "    context.logger.info(\"任务开始\")\n" +
  "    # 读取“凭据绑定”中配置的密码，不要把真实密码直接写进代码\n" +
  "    password = context.secrets.get(\"PASSWORD\")\n" +
  "    try:\n" +
  "        # 在这里使用 password 调用目标系统，但不要打印 password\n" +
  "        return {\"message\": \"hello from DLR\", \"input\": input}\n" +
  "    finally:\n" +
  "        context.logger.info(\"任务结束\")\n";

const WEBHOOK_STARTER_CODE =
  "def handle(context, input):\n" +
  "    context.logger.info(\"收到 Webhook 请求\")\n" +
  "    # 入口 Authorization: Bearer Token 已由平台校验，不会注入 context.secrets\n" +
  "    return {\"received\": True, \"data\": input}\n";

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
    timeout_seconds: 300,
    runtime_worker_id: 1,
    latest_version_id: null,
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
  monacoHarness.reset();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  // Restore the production fallback pace for tests that tightened it.
  FALLBACK_POLICY.pollIntervalMs = 3000;
  FALLBACK_POLICY.maxPolls = 60;
  RUNTIME_REFRESH_POLICY.pollIntervalMs = 3000;
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
  expect(valueOf("code-editor")).not.toContain("context.secrets.get(\"TOKEN\")");
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
    {
      method: "PUT",
      match: "/api/adapters/1/schedule",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}");
        return {
          body: {
            adapter_id: 1,
            enabled: payload.enabled,
            cron: payload.cron,
            timezone: payload.timezone,
            input: payload.input,
            next_run_at: null,
            updated_at: "2026-08-15T00:00:00Z",
          },
        };
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  fireEvent.click(await screen.findByLabelText("定时运行"));
  // M5.5.11：选择“定时运行”后立即呈现 Cron / Timezone / Input / 定时状态。
  await screen.findByTestId("task-schedule-cron");
  fireEvent.click(screen.getByTestId("save-task-runtime"));

  await screen.findByTestId("task-schedule-next-run");
  expect(screen.queryByTestId("enable-task-schedule")).toBeNull();
  expect(screen.getByTestId("header-task-schedule-toggle").textContent).toContain("启用定时");
  const globalActionLabels = Array.from(document.querySelectorAll(".workbench-controls button")).map(
    (button) => button.textContent?.replace(/\s/g, "") ?? "",
  );
  expect(globalActionLabels.indexOf("启用定时")).toBeLessThan(globalActionLabels.indexOf("立即运行一次"));
  const patchCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
  );
  expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
    runtime_worker_id: 1,
    run_mode: "schedule",
    timeout_seconds: 300,
  });
  // 统一保存同时把 Cron/Timezone/Input 以停用状态落库（Schedule 尚未启用）。
  const putCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url) === "/api/adapters/1/schedule" && init?.method === "PUT",
  );
  expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({
    enabled: false,
    cron: "*/5 * * * *",
    timezone: "Asia/Shanghai",
    input: {},
  });
});

it("loads an existing disabled Schedule before saving a manual-to-schedule switch", async () => {
  const manual = makeAdapter({
    adapter_type: "task",
    run_mode: "manual",
    latest_version_id: 10,
    runtime_worker_id: 1,
  });
  const scheduled = { ...manual, run_mode: "schedule" as const };
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [manual] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [makeVersion()] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: makeVersion() }) },
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({
        body: {
          adapter_id: 1,
          enabled: false,
          cron: "0 9 * * *",
          timezone: "UTC",
          input: { preserved: true },
          next_run_at: null,
          updated_at: "2026-08-15T00:00:00Z",
        },
      }),
    },
    { method: "PATCH", match: "/api/adapters/1", respond: () => ({ body: scheduled }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  fireEvent.click(await screen.findByLabelText("定时运行"));

  // 初始 manual 模式没有加载 Schedule；切换表单态后必须先读取并展示
  // 已存在的停用配置，不能把 null 当成“确认未配置”。
  await waitFor(() => expect(valueOf("task-schedule-cron")).toBe("0 9 * * *"));
  expect(valueOf("task-schedule-timezone")).toBe("UTC");
  expect(valueOf("task-schedule-input")).toContain('"preserved": true');

  fireEvent.click(screen.getByTestId("save-task-runtime"));
  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
      ),
    ).toBe(true);
  });
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/schedule" && init?.method === "PUT",
    ),
  ).toBe(false);
});

it("blocks the top Schedule enable action until changed runtime settings are saved", async () => {
  let adapter = makeAdapter({
    run_mode: "schedule",
    latest_version_id: 10,
    runtime_worker_id: 1,
  });
  let schedule = {
    adapter_id: 1,
    enabled: false,
    cron: "*/5 * * * *",
    timezone: "Asia/Shanghai",
    input: {},
    next_run_at: null,
    updated_at: "2026-08-15T00:00:00Z",
  };
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [makeVersion()] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: makeVersion() }) },
    { method: "GET", match: "/api/adapters/1/schedule", respond: () => ({ body: schedule }) },
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: (body) => {
        adapter = { ...adapter, ...JSON.parse(body ?? "{}") };
        return { body: adapter };
      },
    },
    {
      method: "PUT",
      match: "/api/adapters/1/schedule",
      respond: (body) => {
        schedule = { ...schedule, ...JSON.parse(body ?? "{}") };
        return { body: schedule };
      },
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  await screen.findByTestId("task-schedule-cron");

  fireEvent.change(screen.getByTestId("task-schedule-cron"), { target: { value: "0 * * * *" } });
  const enable = screen.getByTestId("header-task-schedule-toggle") as HTMLButtonElement;
  expect(enable.disabled).toBe(true);
  expect(enable.closest(".action-with-reason")?.getAttribute("aria-label")).toContain(
    "运行设置有未保存修改，请先保存运行配置。",
  );
  expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/adapters/1/schedule" && init?.method === "PUT")).toBe(false);

  fireEvent.click(screen.getByTestId("save-task-runtime"));
  await waitFor(() => expect((screen.getByTestId("header-task-schedule-toggle") as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByTestId("header-task-schedule-toggle"));
  await waitFor(() => expect(screen.getByTestId("header-task-schedule-toggle").textContent).toContain("停用定时"));
  expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/adapters/1/schedule" && init?.method === "PUT" && JSON.parse(String(init?.body)).enabled === true)).toBe(true);
});

it("switches manual/schedule fields immediately and keeps run-once out of run settings (M5.5.11)", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, runtime_worker_id: 1 });
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [makeVersion()] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: makeVersion() }) },
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({ status: 404, body: { detail: { code: "schedule_not_configured", message: "Schedule is not configured" } } }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));

  // 手动模式：只显示手动配置（输入）与超时，不显示定时字段。
  expect(await screen.findByTestId("task-manual-input")).toBeTruthy();
  expect(screen.queryByTestId("task-schedule-cron")).toBeNull();
  // 默认超时 5 分钟（300 秒）预设选中。
  const preset = screen.getByTestId("task-timeout-preset");
  expect(preset.querySelector(".ant-radio-wrapper-checked")?.textContent).toContain("5 分钟");
  expect(preset.textContent).toContain("自定义");
  expect(document.body.textContent).toContain("一次运行超过该时间后，系统将自动停止任务并标记为“超时”。");
  expect(document.body.textContent).not.toContain("此配置不是：");
  // M5.5.11：运行设置内部不得重复“运行一次”。
  expect(screen.queryByTestId("task-run-once")).toBeNull();
  expect(screen.queryByText("立即运行一次")).toBeNull();
  expect(screen.queryByText("手动运行", { selector: "h5" })).toBeNull();

  // 选择“定时运行”后立即显示 Cron / Timezone / Input / 定时状态。
  fireEvent.click(screen.getByLabelText("定时运行"));
  await screen.findByTestId("task-schedule-cron");
  expect(screen.getByTestId("task-schedule-timezone")).toBeTruthy();
  expect(screen.getByTestId("task-schedule-input")).toBeTruthy();
  expect(screen.getByTestId("task-schedule-next-run")).toBeTruthy();
  expect(screen.queryByTestId("enable-task-schedule")).toBeNull();
  expect(screen.queryByTestId("task-manual-input")).toBeNull();

  // 切回手动：手动字段回来，定时字段消失。
  fireEvent.click(screen.getByLabelText("手动运行"));
  await screen.findByTestId("task-manual-input");
  expect(screen.queryByTestId("task-schedule-cron")).toBeNull();
});

it("saves a custom timeout in seconds and rejects values beyond 24h (M5.5.11)", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, runtime_worker_id: 1 });
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [makeVersion()] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: makeVersion() }) },
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}");
        return { body: { ...adapter, ...payload } };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));

  // 自定义 7200 秒（2 小时）并保存：PATCH 携带秒值。
  fireEvent.click(await screen.findByLabelText("自定义"));
  fireEvent.change(await screen.findByTestId("task-timeout-custom"), {
    target: { value: "7200" },
  });
  fireEvent.click(screen.getByTestId("save-task-runtime"));
  await waitFor(() => {
    const patchCall = fetchMock.mock.calls.find(
      ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
    );
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      runtime_worker_id: 1,
      run_mode: "manual",
      timeout_seconds: 7200,
    });
  });
  // 等待第一次保存完全结束（loading 复位），再继续第二次交互。
  await waitFor(() => {
    expect(
      (screen.getByTestId("save-task-runtime") as HTMLButtonElement).className,
    ).not.toContain("ant-btn-loading");
  });

  // 超过 24 小时（86400 秒）的值在输入层被钳制到 86400（最大 24 小时）。
  const callsBefore = fetchMock.mock.calls.length;
  fireEvent.change(screen.getByTestId("task-timeout-custom"), {
    target: { value: "86401" },
  });
  fireEvent.blur(screen.getByTestId("task-timeout-custom"));
  await waitFor(() => {
    expect((screen.getByTestId("task-timeout-custom") as HTMLInputElement).value).toBe("86400");
  });
  fireEvent.click(screen.getByTestId("save-task-runtime"));
  await waitFor(() => {
    const patchCall = fetchMock.mock.calls
      .slice(callsBefore)
      .find(([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH");
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      runtime_worker_id: 1,
      run_mode: "manual",
      timeout_seconds: 86400,
    });
  });
});

it("saves the timeout only without overwriting an existing Schedule (M5.5.11)", async () => {
  const adapter = makeAdapter({
    run_mode: "schedule",
    latest_version_id: 10,
    runtime_worker_id: 1,
  });
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [makeVersion()] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: makeVersion() }) },
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({
        body: { adapter_id: 1, enabled: false, cron: "0 9 * * *", timezone: "UTC", input: { real: true }, next_run_at: null, updated_at: "2026-08-15T00:00:00Z" },
      }),
    },
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}");
        return { body: { ...adapter, ...payload } };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));

  // 等真实 Schedule 加载完成（表单值 = 线上值）。
  await waitFor(() => expect(valueOf("task-schedule-cron")).toBe("0 9 * * *"));
  // 只修改超时，不改定时字段。
  fireEvent.click(screen.getByLabelText("10 分钟"));
  fireEvent.click(screen.getByTestId("save-task-runtime"));

  await waitFor(() => {
    const patchCall = fetchMock.mock.calls.find(
      ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
    );
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      runtime_worker_id: 1,
      run_mode: "schedule",
      timeout_seconds: 600,
    });
  });
  // 定时字段未被修改：不得整体 PUT 覆盖线上 Schedule。
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/schedule" && init?.method === "PUT",
    ),
  ).toBe(false);
});

it("skips the Schedule PUT while Schedule is still loading or failed to load (M5.5.11)", async () => {
  const adapter = makeAdapter({
    run_mode: "schedule",
    latest_version_id: 10,
    runtime_worker_id: 1,
  });
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [makeVersion()] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: makeVersion() }) },
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({ status: 500, body: { detail: { code: "boom", message: "boom" } } }),
    },
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}");
        return { body: { ...adapter, ...payload } };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));

  // Schedule 加载失败（表单仍是默认值）：保存超时时不得把默认值 PUT 上去。
  fireEvent.click(await screen.findByLabelText("10 分钟"));
  fireEvent.click(screen.getByTestId("save-task-runtime"));

  await waitFor(() => {
    const patchCall = fetchMock.mock.calls.find(
      ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
    );
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      runtime_worker_id: 1,
      run_mode: "schedule",
      timeout_seconds: 600,
    });
  });
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/schedule" && init?.method === "PUT",
    ),
  ).toBe(false);
  await screen.findByText(
    "运行配置已保存；但 Schedule 尚未加载完成（或加载失败），定时字段未保存，请刷新后重试。",
  );
});

it("locks the run config save and timeout while Schedule is enabled (M5.5.11)", async () => {
  const adapter = makeAdapter({
    run_mode: "schedule",
    latest_version_id: 10,
    runtime_worker_id: 1,
    runtime_locked: true,
  });
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [makeVersion()] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: makeVersion() }) },
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({
        body: { adapter_id: 1, enabled: true, cron: "*/5 * * * *", timezone: "Asia/Shanghai", input: {}, next_run_at: "2026-08-15T01:00:00Z", updated_at: "2026-08-15T00:00:00Z" },
      }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));

  await screen.findByTestId("task-schedule-cron");
  expect((screen.getByTestId("save-task-runtime") as HTMLButtonElement).disabled).toBe(true);
  expect(
    (screen.getByTestId("task-timeout-preset").querySelector("input") as HTMLInputElement).disabled,
  ).toBe(true);
  expect(screen.getByTestId("task-runtime-locked")).toBeTruthy();
  // 锁定期间任何 PATCH 都不应发出。
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
    ),
  ).toBe(false);
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
      respond: () => enabled
        ? { body: { adapter_id: 1, enabled: true, cron: "*/5 * * * *", timezone: "Asia/Shanghai", input: {}, next_run_at: "2026-08-15T01:00:00Z", updated_at: "2026-08-15T00:00:00Z" } }
        : { body: { adapter_id: 1, enabled: false, cron: "*/5 * * * *", timezone: "Asia/Shanghai", input: {}, next_run_at: null, updated_at: "2026-08-15T00:00:00Z" } },
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
  fireEvent.click(await screen.findByTestId("header-task-schedule-toggle"));
  await waitFor(() => expect(screen.getByTestId("header-task-schedule-toggle").textContent).toContain("停用定时"));
  fireEvent.click(screen.getByRole("tab", { name: "编辑" }));
  await waitFor(() => expect((screen.getByTestId("code-editor") as HTMLTextAreaElement).disabled).toBe(true));
  // M5.5.9：运行中只保留低干扰提示；不再展示大块黄色说明、复制引导或修订号。
  expect(screen.getByTestId("task-active-execution").textContent).toContain(
    "适配器正在运行，编辑与运行配置已锁定。",
  );
  expect(document.body.textContent).not.toContain("如需升级，请复制为新的适配器");
  expect(screen.queryByTestId("header-clone-adapter")).toBeNull();
  expect(document.body.textContent).not.toContain("修订版");
  expect(screen.getByTestId("save-version").closest(".action-with-reason")?.getAttribute("aria-label")).toContain(
    "定时已启用，请先停用定时后再保存",
  );
  // 复制适配器仍可从设置抽屉进入。
  fireEvent.click(screen.getByTestId("adapter-settings"));
  fireEvent.click(await screen.findByTestId("clone-adapter"));
  const dialogs = await screen.findAllByRole("dialog");
  const cloneDialog = dialogs.find(
    (dialog) => within(dialog).queryByTestId("clone-adapter-name") !== null,
  );
  if (!cloneDialog) {
    throw new Error("clone dialog not found");
  }
  expect(within(cloneDialog).getByText("执行历史不会复制；新适配器创建后保持停止，不会自动运行。")).toBeTruthy();
  expect((within(cloneDialog).getByTestId("clone-adapter-name") as HTMLInputElement).value).toBe("adapter-a-copy");
  fireEvent.click(within(cloneDialog).getByRole("button", { name: /取\s*消/ }));
  // 关闭设置抽屉后继续验证解锁（抽屉 destroyOnHidden，内容随关闭卸载）。
  const drawerClose = document.querySelector(".ant-drawer-close");
  if (!(drawerClose instanceof HTMLElement)) {
    throw new Error("settings drawer close button not found");
  }
  fireEvent.click(drawerClose);
  await waitFor(() => expect(screen.queryByTestId("clone-adapter")).toBeNull());

  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  fireEvent.click(screen.getByTestId("header-task-schedule-toggle"));
  await waitFor(() => expect(screen.getByTestId("header-task-schedule-toggle").textContent).toContain("启用定时"));
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
    expectedReason: "请先保存适配器，再启用定时。",
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

  const enable = await screen.findByTestId("header-task-schedule-toggle") as HTMLButtonElement;
  expect(enable.disabled).toBe(true);
  expect(enable.closest(".action-with-reason")?.getAttribute("aria-label")).toContain(expectedReason);
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
  expect(screen.getByTestId("login-error").textContent).toContain("登录失败，请检查 Token 后重试");
  expect(screen.getByTestId("login-error").textContent).toContain("错误码：unauthorized");
  expect(screen.getByTestId("login-error").textContent).not.toContain("Invalid credentials");
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
    expect(screen.getByTestId("control-status").textContent).toBe("控制服务正常");
  });
});

it("shows degraded when control returns 503 with a valid health payload", async () => {
  stubFetch([
    healthRoute({ status: "degraded", database: false }, 503),
    emptyAdaptersRoute,
  ]);
  render(<App />);
  await waitFor(() => {
    expect(screen.getByTestId("control-status").textContent).toBe("控制服务降级");
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
    expect(screen.getByTestId("control-status").textContent).toBe("控制服务不可达");
  });
});

it("does not show ok for the contradictory payload {status: ok, database: false}", async () => {
  stubFetch([healthRoute({ status: "ok", database: false }), emptyAdaptersRoute]);
  render(<App />);
  await waitFor(() => {
    expect(screen.getByTestId("control-status").textContent).toBe("控制服务不可达");
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
  await screen.findByText("暂无适配器");

  fireEvent.click(screen.getByTestId("show-create-form"));
  await screen.findByTestId("new-adapter-name");
  expect(screen.getByRole("textbox", { name: "适配器名称" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "适配器描述" })).toBeTruthy();
  expect(screen.getByRole("radiogroup", { name: "适配器开发语言" })).toBeTruthy();
  fireEvent.click(screen.getByTestId("create-adapter"));
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => url === "/api/adapters" && init?.method === "POST",
    ),
  ).toBe(false);
  fireEvent.change(screen.getByTestId("new-adapter-name"), { target: { value: "cmdb-sync" } });
  fireEvent.change(screen.getByTestId("new-adapter-description"), {
    target: { value: "sync cmdb" },
  });
  fireEvent.click(screen.getByTestId("create-adapter"));

  // Created adapter becomes selected; metadata moved to the settings drawer.
  await screen.findByRole("heading", { name: "cmdb-sync" });
  expect(screen.queryByTestId("new-adapter-name")).toBeNull();
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
  await screen.findByText("暂无适配器");
  fireEvent.click(screen.getByTestId("show-create-form"));
  fireEvent.change(screen.getByTestId("new-adapter-name"), {
    target: { value: "node-adapter" },
  });
  fireEvent.click(screen.getByText("JavaScript"));
  fireEvent.click(screen.getByRole("radio", { name: "Webhook 适配器" }));
  fireEvent.click(screen.getByTestId("create-adapter"));

  const editor = await screen.findByTestId("code-editor");
  expect(JSON.parse(createBody).language).toBe("javascript");
  expect(JSON.parse(createBody).adapter_type).toBe("webhook");
  expect((editor as HTMLTextAreaElement).value).toContain("export async function handle");
  expect(editor.getAttribute("data-monaco-language")).toBe("javascript");
  expect(screen.getByText("JavaScript 依赖")).toBeTruthy();

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

it("removes the 运行参数（JSON） entry from the edit page and saves the inherited config", async () => {
  const adapter = makeAdapter();
  const versions: VersionSummary[] = [];
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: versions }) },
    {
      method: "POST",
      match: "/api/adapters/1/versions",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}") as {
          code: string;
          requirements: string;
          runtime_config: Record<string, unknown>;
        };
        const saved = makeVersion({ code: payload.code, runtime_config: payload.runtime_config });
        versions.push(saved);
        adapter.latest_version_id = saved.id;
        return { status: 201, body: saved };
      },
    },
    { method: "GET", match: "/api/adapters/1", respond: () => ({ body: adapter }) },
  ]);
  render(<App />);
  await selectFirstAdapter();

  // M5.5.9：运行参数（JSON）退出用户主流程，编辑页只保留依赖与凭据绑定。
  expect(screen.queryByText("运行参数（JSON）")).toBeNull();
  expect(screen.queryByTestId("runtime-config-input")).toBeNull();

  fireEvent.change(screen.getByTestId("code-editor"), { target: { value: "def f():\n    pass\n" } });
  fireEvent.click(screen.getByTestId("save-version"));

  await screen.findByText("适配器已保存");
  const saveCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url).endsWith("/versions") && init?.method === "POST",
  );
  expect(JSON.parse(String(saveCall?.[1]?.body))).toEqual({
    code: "def f():\n    pass\n",
    requirements: "",
    runtime_config: {},
  });
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
  fireEvent.click(screen.getByTestId("save-version"));

  await screen.findByText("适配器已保存");
  const saveCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url).endsWith("/versions") && init?.method === "POST",
  );
  expect(JSON.parse(String(saveCall?.[1]?.body))).toEqual({
    code: "def handle(context, input):\n    return {'done': True}\n",
    requirements: "requests==2.32.0",
    runtime_config: {},
  });
  expect(await screen.findByText("适配器已保存")).toBeTruthy();
});

it("asks for a compatible Worker on first Save when several are available", async () => {
  let adapter = makeAdapter({ runtime_worker_id: null });
  const versions: VersionSummary[] = [];
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [
          { id: 1, name: "worker-a", status: "online", last_heartbeat: "", capabilities: ["python"] },
          { id: 2, name: "worker-b", status: "online", last_heartbeat: "", capabilities: ["python"] },
          { id: 3, name: "worker-js", status: "online", last_heartbeat: "", capabilities: ["javascript"] },
        ],
      }),
    },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: versions }) },
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: (body) => {
        adapter = { ...adapter, runtime_worker_id: JSON.parse(body ?? "{}").runtime_worker_id };
        return { body: adapter };
      },
    },
    {
      method: "POST",
      match: "/api/adapters/1/versions",
      respond: () => {
        const saved = makeVersion();
        versions.push(saved);
        adapter = { ...adapter, latest_version_id: saved.id };
        return { status: 201, body: saved };
      },
    },
    { method: "GET", match: "/api/adapters/1", respond: () => ({ body: adapter }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByTestId("save-version"));

  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByText("第一次保存需要确定运行节点。后续可在“运行设置”中查看或修改。")).toBeTruthy();
  fireEvent.mouseDown(within(dialog).getByRole("combobox"));
  fireEvent.click(await screen.findByText("worker-b"));
  fireEvent.click(within(dialog).getByRole("button", { name: /保\s*存/ }));

  await screen.findByText("适配器已保存");
  const patchCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
  );
  expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({ runtime_worker_id: 2 });
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/versions" && init?.method === "POST",
    ),
  ).toBe(true);
});

it("automatically selects the only compatible online Worker on first Save", async () => {
  let adapter = makeAdapter({ runtime_worker_id: null });
  const versions: VersionSummary[] = [];
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [
          { id: 4, name: "only-python", status: "online", last_heartbeat: "", capabilities: ["python"] },
          { id: 5, name: "offline-python", status: "offline", last_heartbeat: "", capabilities: ["python"] },
        ],
      }),
    },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: versions }) },
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: (body) => {
        adapter = { ...adapter, runtime_worker_id: JSON.parse(body ?? "{}").runtime_worker_id };
        return { body: adapter };
      },
    },
    {
      method: "POST",
      match: "/api/adapters/1/versions",
      respond: () => {
        const saved = makeVersion();
        versions.push(saved);
        adapter = { ...adapter, latest_version_id: saved.id };
        return { status: 201, body: saved };
      },
    },
    { method: "GET", match: "/api/adapters/1", respond: () => ({ body: adapter }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByTestId("save-version"));

  await screen.findByText("适配器已保存");
  expect(screen.queryByRole("dialog")).toBeNull();
  const patchCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
  );
  expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({ runtime_worker_id: 4 });
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
  await waitFor(() => expect(valueOf("code-editor")).toBe("edited"));

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
  expect(screen.getByTestId("error-banner").textContent).toContain("请求失败");
  expect(screen.getByTestId("error-banner").textContent).toContain("错误码：boom");
  expect(screen.getByTestId("error-banner").textContent).not.toContain("server exploded");
  expect(screen.queryAllByTestId("adapter-item")).toHaveLength(0);
});

it("blocks duplicate adapter creation with a clear Chinese prompt and no request", async () => {
  const fetchMock = stubFetch([
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

  // M5.5.9：创建同名活跃适配器被前端预检拦截并给出明确中文提示，不发请求。
  fireEvent.click(screen.getByTestId("show-create-form"));
  await screen.findByTestId("new-adapter-name");
  fireEvent.change(screen.getByTestId("new-adapter-name"), { target: { value: "adapter-a" } });
  fireEvent.click(screen.getByTestId("create-adapter"));

  await screen.findByText("已存在同名适配器，请使用其他名称。");
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters" && init?.method === "POST",
    ),
  ).toBe(false);
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
  expect(screen.getByTestId("error-banner").textContent).toContain("适配器已保存");

  // The saved version is acknowledged: not dirty, selected, and marked latest,
  // so the user is never encouraged to repeat an already-successful save.
  expect(valueOf("code-editor")).toBe("saved code");
  // 未保存修改已清除：运行按钮不再被“请先保存当前修改，再运行。”门禁拦截。
  await waitFor(() => {
    expect((screen.getByTestId("header-task-run-once") as HTMLButtonElement).disabled).toBe(false);
  });
  expect(
    (screen.getByTestId("header-task-run-once") as HTMLButtonElement)
      .closest(".action-with-reason")
      ?.getAttribute("data-disabled-reason") ?? "",
  ).not.toContain("请先保存当前修改");
});

// --- Review regressions: create form only closes on real success -------------

it("blocks renaming to an existing active adapter name with a clear prompt (M5.5.9)", async () => {
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({ body: [makeAdapter(), makeAdapter({ id: 2, name: "adapter-b" })] }),
    },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [] }) },
  ]);
  render(<App />);
  await selectFirstAdapter();

  fireEvent.click(screen.getByTestId("adapter-settings"));
  await screen.findByTestId("adapter-name");
  // 精确同名与 trim 后同名都被前端预检拦截，不发 PATCH。
  fireEvent.change(screen.getByTestId("adapter-name"), { target: { value: "adapter-b" } });
  fireEvent.click(screen.getByTestId("update-details"));
  expect(await screen.findByText("已存在同名适配器，请使用其他名称。")).toBeTruthy();

  fireEvent.change(screen.getByTestId("adapter-name"), { target: { value: "  adapter-b  " } });
  fireEvent.click(screen.getByTestId("update-details"));
  // 第二次点击再次给出提示（trim 后仍冲突）。
  await waitFor(() =>
    expect(screen.getAllByText("已存在同名适配器，请使用其他名称。").length).toBeGreaterThanOrEqual(2),
  );
  expect(valueOf("adapter-name")).toBe("  adapter-b  ");
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
    ),
  ).toBe(false);
});

it("keeps the create form and its inputs when the backend rejects a duplicate name", async () => {
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
  // 并发场景：本地列表尚未刷新，后端权威校验拒绝同名。
  fireEvent.change(screen.getByTestId("new-adapter-name"), { target: { value: "race-dup" } });
  fireEvent.change(screen.getByTestId("new-adapter-description"), {
    target: { value: "keep me" },
  });
  fireEvent.click(screen.getByTestId("create-adapter"));

  await screen.findByText("已存在同名适配器，请使用其他名称。");
  // The form stays open with the user's input still editable.
  expect(screen.getByTestId("new-adapter-name")).toBeTruthy();
  expect(valueOf("new-adapter-name")).toBe("race-dup");
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
  await waitFor(() => expect(valueOf("code-editor")).toBe("edited"));

  const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
  fireEvent.click(screen.getByTestId("show-create-form"));
  await screen.findByTestId("new-adapter-name");
  fireEvent.change(screen.getByTestId("new-adapter-name"), { target: { value: "new-one" } });
  fireEvent.click(screen.getByTestId("create-adapter"));

  await waitFor(() => expect(confirmSpy).toHaveBeenCalled());
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

  fireEvent.change(screen.getByTestId("code-editor"), { target: { value: "edited code" } });
  fireEvent.click(screen.getByTestId("save-version"));

  // While Save is pending, every editing/navigation surface is locked.
  await waitFor(() => {
    expect((screen.getByTestId("code-editor") as HTMLTextAreaElement).disabled).toBe(true);
  });
  expect((screen.getByTestId("requirements-input") as HTMLTextAreaElement).disabled).toBe(true);
  expect((screen.getByTestId("save-version") as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getAllByTestId("adapter-item")[0] as HTMLButtonElement).disabled).toBe(true);

  // Save completes with exactly the snapshot that existed when it started.
  const savedVersion = makeVersion({ code: "edited code", requirements: "", runtime_config: {} });
  versions.push(savedVersion);
  resolveSave?.(savedVersion);
  await screen.findByText("适配器已保存");

  const saveCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url).endsWith("/versions") && init?.method === "POST",
  );
  const sentPayload = JSON.parse(String(saveCall?.[1]?.body)) as { code: string };
  expect(sentPayload.code).toBe("edited code");
  expect(valueOf("code-editor")).toBe("edited code");
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

  // The save is still acknowledged (not dirty) while the failed Adapter
  // refresh is reported separately; the server-owned updated_at is never
  // synthesized from the version's created_at.
  await waitFor(() =>
    expect(screen.getByTestId("error-banner").textContent).toContain("适配器已保存"),
  );
  expect(screen.getByTestId("error-banner").textContent).toContain("刷新适配器失败");
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

it("runs a Task from the Workbench header and follows it in the 实时日志 tab", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, runtime_worker_id: 1 });
  const pending = makeExecution();
  const succeeded = makeExecution({
    status: "succeeded",
    worker_id: 1,
    target_worker_id: 1,
    stdout: "[2026-08-17 10:30:00] 任务开始\n[2026-08-17 10:30:01] 任务结束\n",
    output: { ok: true },
    output_size: 11,
    ended_at: "2026-08-15T00:00:02Z",
    duration_ms: 1000,
  });
  const fetchMock = stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [{ id: 1, name: "task-worker", status: "online", last_heartbeat: "", capabilities: ["python"] }],
      }),
    },
    { method: "POST", match: "/api/adapters/1/executions", respond: () => ({ status: 201, body: pending }) },
    { method: "GET", match: "/api/adapters/1", respond: () => ({ body: adapter }) },
    {
      method: "GET",
      match: "/api/executions/5/events",
      respond: () => ({
        stream: `event: log\ndata: ${JSON.stringify({ stream: "stdout", chunk: "[2026-08-17 10:30:00] 任务开始\\n" })}\n\nevent: execution\ndata: ${JSON.stringify(succeeded)}\n\n`,
      }),
    },
    { method: "GET", match: "/api/executions/5", respond: () => ({ body: succeeded }) },
  ]);

  render(<App />);
  await selectFirstAdapter();
  const runButton = await screen.findByTestId("header-task-run-once") as HTMLButtonElement;
  await waitFor(() => expect(runButton.disabled).toBe(false));
  fireEvent.click(runButton);

  // M5.5.10：手动运行自动切换到「实时日志」Tab，统一视图展示全部日志。
  const workspace = await screen.findByTestId("live-log-workspace");
  expect(screen.getByRole("tab", { name: "实时日志" }).getAttribute("aria-selected")).toBe("true");
  await waitFor(() => {
    expect(screen.getByTestId("live-log").textContent).toContain("任务开始");
    expect(screen.getByTestId("live-log").textContent).toContain("任务结束");
  });
  expect(workspace.textContent).not.toContain("执行 #");
  expect(workspace.textContent).not.toContain("Execution #");
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/executions" && init?.method === "POST",
    ),
  ).toBe(true);
});

it("reopens the same server log after closing the live-to-history drawer", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, runtime_worker_id: 1 });
  const pending = makeExecution();
  const savedLog = Array.from({ length: 2001 }, (_, index) => `line-${index}`).join("\n");
  const succeeded = makeExecution({
    status: "succeeded",
    worker_id: 1,
    target_worker_id: 1,
    stdout: savedLog,
    output: { ok: true },
    output_size: 11,
    ended_at: "2026-08-15T00:00:02Z",
    duration_ms: 1000,
  });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [{ id: 1, name: "task-worker", status: "online", last_heartbeat: "", capabilities: ["python"] }],
      }),
    },
    { method: "POST", match: "/api/adapters/1/executions", respond: () => ({ status: 201, body: pending }) },
    { method: "GET", match: "/api/adapters/1", respond: () => ({ body: adapter }) },
    {
      method: "GET",
      match: "/api/executions/5/events",
      respond: () => ({
        stream: `event: execution\ndata: ${JSON.stringify(succeeded)}\n\n`,
      }),
    },
    { method: "GET", match: "/api/executions/5", respond: () => ({ body: succeeded }) },
    {
      method: "GET",
      match: /\/api\/adapters\/1\/executions\?/,
      respond: () => ({ body: { items: [makeSummary({ id: 5 })], next_before_id: null } }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  await waitFor(() => {
    expect((screen.getByTestId("header-task-run-once") as HTMLButtonElement).disabled).toBe(false);
  });
  fireEvent.click(screen.getByTestId("header-task-run-once"));

  await screen.findByTestId("live-log-view-server");
  fireEvent.click(screen.getByTestId("live-log-view-server"));
  await screen.findByTestId("detail-input");

  const closeButton = document.querySelector(".ant-drawer-close");
  if (!(closeButton instanceof HTMLButtonElement)) {
    throw new Error("Execution detail drawer close button not found");
  }
  fireEvent.click(closeButton);
  fireEvent.click(screen.getByRole("tab", { name: "实时日志" }));
  await screen.findByTestId("live-log-view-server");

  // The handoff request is one-shot: after the drawer closes, the same
  // execution id must be accepted again instead of React bailing on setState.
  fireEvent.click(screen.getByTestId("live-log-view-server"));
  expect(await screen.findByTestId("detail-input")).toBeTruthy();
});

it("blocks running while unsaved edits exist and unblocks after Save (M5.5.9)", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, runtime_worker_id: 1 });
  const versions: VersionSummary[] = [
    { id: 10, adapter_id: 1, seq: 1, created_at: "2026-08-15T00:00:00Z" },
  ];
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: versions }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: makeVersion() }) },
    {
      method: "POST",
      match: "/api/adapters/1/versions",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}") as { code: string };
        const saved = makeVersion({ id: 11, seq: 2, code: payload.code });
        versions.push({ id: saved.id, adapter_id: 1, seq: saved.seq, created_at: saved.created_at });
        adapter.latest_version_id = saved.id;
        return { status: 201, body: saved };
      },
    },
    { method: "GET", match: "/api/adapters/1", respond: () => ({ body: adapter }) },
  ]);
  render(<App />);
  await selectFirstAdapter();

  // 等待内容加载完成（save 可用），避免加载完成时覆盖编辑快照。
  await waitFor(() =>
    expect((screen.getByTestId("save-version") as HTMLButtonElement).disabled).toBe(false),
  );
  const runButton = await screen.findByTestId("header-task-run-once") as HTMLButtonElement;
  await waitFor(() => expect(runButton.disabled).toBe(false));

  // 未保存修改：运行被门禁拦截并提示先保存，不发执行请求。
  fireEvent.change(screen.getByTestId("code-editor"), { target: { value: "unsaved edit" } });
  await waitFor(() => {
    const freshButton = screen.getByTestId("header-task-run-once") as HTMLButtonElement;
    expect(freshButton.disabled).toBe(true);
  });
  expect(
    (screen.getByTestId("header-task-run-once") as HTMLButtonElement)
      .closest(".action-with-reason")
      ?.getAttribute("aria-label"),
  ).toContain("请先保存当前修改，再运行。");
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/executions" && init?.method === "POST",
    ),
  ).toBe(false);

  // 保存后门禁解除，可直接运行。
  fireEvent.click(screen.getByTestId("save-version"));
  await screen.findByText("适配器已保存");
  await waitFor(() => {
    const freshButton = screen.getByTestId("header-task-run-once") as HTMLButtonElement;
    expect(freshButton.disabled).toBe(false);
  });
  expect(
    (screen.getByTestId("header-task-run-once") as HTMLButtonElement)
      .closest(".action-with-reason")
      ?.getAttribute("aria-label") ?? "",
  ).not.toContain("请先保存当前修改");
});

it("announces a background Schedule run without leaving the execution history tab", async () => {
  const runningAdapter = makeAdapter({
    latest_version_id: 10,
    runtime_worker_id: 1,
    run_mode: "schedule",
    runtime_locked: true,
    running_execution_id: 5,
  });
  const unlockedAdapter = { ...runningAdapter, runtime_locked: false, running_execution_id: null };
  const running = makeExecution({ trigger: "schedule", status: "running" });
  const succeeded = makeExecution({
    trigger: "schedule",
    status: "succeeded",
    stdout: "[2026-08-17 10:30:00] 任务开始\n[2026-08-17 10:30:01] 任务结束\n",
    ended_at: "2026-08-15T00:00:02Z",
  });
  let executionReads = 0;
  stubFetch([
    ...consoleWithVersionRoutes(runningAdapter, makeVersion()),
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [{ id: 1, name: "task-worker", status: "online", last_heartbeat: "", capabilities: ["python"] }],
      }),
    },
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({
        body: { adapter_id: 1, enabled: true, cron: "* * * * *", timezone: "UTC", input: {}, next_run_at: "2026-08-15T00:01:00Z", updated_at: "2026-08-15T00:00:00Z" },
      }),
    },
    {
      method: "GET",
      match: "/api/executions/5",
      respond: () => {
        executionReads += 1;
        return { body: executionReads === 1 ? running : succeeded };
      },
    },
    {
      method: "GET",
      match: "/api/executions/5/events",
      respond: () => ({
        stream: `event: execution\ndata: ${JSON.stringify(succeeded)}\n\n`,
      }),
    },
    { method: "GET", match: "/api/adapters/1", respond: () => ({ body: unlockedAdapter }) },
    {
      method: "GET",
      match: /\/api\/adapters\/1\/executions\?/,
      respond: () => ({ body: { items: [makeSummary({ id: 5, trigger: "schedule" })], next_before_id: null } }),
    },
  ]);

  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "执行记录" }));

  await screen.findByText("定时执行已开始，可在「实时日志」标签查看本次运行。");
  expect(screen.getByRole("tab", { name: "执行记录" }).getAttribute("aria-selected")).toBe("true");
  expect(await screen.findAllByTestId("history-row")).toHaveLength(1);
});

it("keeps Schedule disablement separate from cancelling the current Execution", async () => {
  const onToggleSchedule = vi.fn();
  const onStopExecution = vi.fn();
  const adapter = makeAdapter({
    latest_version_id: 10,
    runtime_worker_id: 1,
    run_mode: "schedule",
    runtime_locked: true,
    running_execution_id: 92,
  });

  const commonProps = {
    runtimeWorker: null,
    dirty: false,
    busy: false,
    contentReady: true,
    onSave: vi.fn(),
    onOpenSettings: vi.fn(),
    onRunOnce: vi.fn(),
    onStopExecution,
    onToggleSchedule,
  };
  const { rerender } = render(
    <TaskWorkbenchHeader
      {...commonProps}
      adapter={adapter}
      runtimeState={{
        scheduleEnabled: true,
        loading: false,
        activeExecution: true,
        canRun: false,
        scheduleEnableBlockedReason: null,
      }}
    />,
  );

  const disableSchedule = screen.getByTestId("header-task-schedule-toggle") as HTMLButtonElement;
  expect(disableSchedule.textContent).toBe("停用定时");
  expect(disableSchedule.disabled).toBe(false);
  expect((screen.getByTestId("header-task-stop") as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(disableSchedule);
  expect(onToggleSchedule).toHaveBeenCalledOnce();
  expect(onStopExecution).not.toHaveBeenCalled();

  rerender(
    <TaskWorkbenchHeader
      {...commonProps}
      adapter={adapter}
      runtimeState={{
        scheduleEnabled: false,
        loading: false,
        activeExecution: true,
        canRun: false,
        scheduleEnableBlockedReason: null,
      }}
    />,
  );
  const lockedEnable = screen.getByTestId("header-task-schedule-toggle") as HTMLButtonElement;
  expect(lockedEnable.textContent).toBe("启用定时");
  expect(lockedEnable.disabled).toBe(true);
  expect(lockedEnable.closest(".action-with-reason")?.getAttribute("aria-label")).toContain(
    "当前执行仍在运行，请等待终态或停止当前执行后再启用定时",
  );
  fireEvent.click(lockedEnable);
  expect(onToggleSchedule).toHaveBeenCalledOnce();

  fireEvent.click(screen.getByTestId("header-task-stop"));
  expect(screen.getByTestId("header-task-stop").textContent).toBe("停止当前执行");
  expect(onStopExecution).toHaveBeenCalledOnce();

  rerender(
    <TaskWorkbenchHeader
      {...commonProps}
      adapter={{ ...adapter, runtime_locked: false, running_execution_id: null }}
      runtimeState={{
        scheduleEnabled: false,
        loading: false,
        activeExecution: false,
        canRun: true,
        scheduleEnableBlockedReason: null,
      }}
    />,
  );
  const unlockedEnable = screen.getByTestId("header-task-schedule-toggle") as HTMLButtonElement;
  expect(unlockedEnable.disabled).toBe(false);
  fireEvent.click(unlockedEnable);
  expect(onToggleSchedule).toHaveBeenCalledTimes(2);
});

it("cancels the authoritative Adapter Execution instead of a stale terminal watcher", async () => {
  const adapter = makeAdapter({
    latest_version_id: 10,
    runtime_worker_id: 1,
    run_mode: "schedule",
    runtime_locked: true,
    running_execution_id: 92,
  });
  const staleTerminal = makeExecution({ id: 91, status: "succeeded" });
  const cancelled = makeExecution({ id: 92, status: "cancelled" });
  const fetchMock = stubFetch([
    {
      method: "GET",
      match: "/api/adapters/1/schedule",
      respond: () => ({
        body: { adapter_id: 1, enabled: true, cron: "* * * * *", timezone: "UTC", input: {}, next_run_at: null, updated_at: "2026-08-15T00:00:00Z" },
      }),
    },
    { method: "POST", match: "/api/executions/92/cancel", respond: () => ({ body: cancelled }) },
    { method: "GET", match: "/api/adapters/1", respond: () => ({ body: { ...adapter, runtime_locked: false, running_execution_id: null } }) },
  ]);

  const runtimeRef = createRef<TaskRunSettingsHandle>();
  render(
    <TaskRunSettingsPanel
      ref={runtimeRef}
      adapter={adapter}
      workers={[]}
      workersLoading={false}
      workersError={null}
      execution={staleTerminal}
      dirty={false}
      onAdapterChange={vi.fn()}
      onExecutionStarted={vi.fn()}
      onRuntimeStateChange={vi.fn()}
      onError={vi.fn()}
    />,
  );

  await waitFor(() => expect(runtimeRef.current).not.toBeNull());
  expect(screen.queryByTestId("task-stop-run")).toBeNull();
  act(() => runtimeRef.current?.stopExecution());
  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => String(url) === "/api/executions/92/cancel" && init?.method === "POST",
      ),
    ).toBe(true);
  });
  expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/executions/91/cancel")).toBe(false);
});

it("lists unfiltered Task execution history with cursor pagination and opens detail", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const pageOne = {
    items: [makeSummary({
      id: 6,
      version_seq: 7,
      worker_id: 3,
      worker_name: "worker-main",
      started_at: "2026-08-15T00:00:01Z",
      ended_at: "2026-08-15T00:00:02Z",
      duration_ms: 1000,
    })],
    next_before_id: 6,
  };
  const pageTwo = {
    items: [makeSummary({
      id: 4,
      version_seq: 6,
      worker_id: 4,
      worker_name: "worker-alt",
      status: "failed",
      started_at: "2026-08-15T00:00:03Z",
      ended_at: null,
    })],
    next_before_id: null,
  };
  const detail = makeExecution({
    id: 6,
    worker_id: 3,
    status: "succeeded",
    input: { k: 1 },
    output: { ok: true },
    stdout: Array.from({ length: 501 }, (_, index) => `line-${index}`).join("\n"),
  });
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

  // M5.5.10：历史列表与详情不再暴露内部版本号与 Execution #N。
  expect(document.body.textContent).not.toContain("v7");
  expect(document.body.textContent).not.toMatch(/执行 #/);

  const [firstRow, secondRow] = screen.getAllByTestId("history-row");
  expect(firstRow.getAttribute("tabindex")).toBe("0");
  firstRow.focus();
  fireEvent.keyDown(firstRow, { key: "Enter" });
  const detailInput = await screen.findByTestId("detail-input");
  expect(detailInput.textContent).toContain('"k": 1');
  const firstRowText = firstRow.textContent ?? "";
  expect(firstRowText).toContain("2026");
  expect(firstRowText).not.toContain("—");
  const drawer = document.querySelector(".ant-drawer-content");
  if (!(drawer instanceof HTMLElement)) {
    throw new Error("Execution detail drawer not found");
  }
  expect(within(drawer).getByText("worker-main")).toBeTruthy();
  expect(within(drawer).getByText("运行 ID：6")).toBeTruthy();
  expect(within(drawer).queryByText(/版本/)).toBeNull();
  expect(within(drawer).queryByText("v7")).toBeNull();
  expect(within(drawer).queryByText("#10")).toBeNull();
  expect(within(drawer).queryByText("#3")).toBeNull();
  expect(within(drawer).getByRole("tab", { name: "执行日志" })).toBeTruthy();
  expect(within(drawer).queryByRole("tab", { name: "实时日志" })).toBeNull();
  fireEvent.click(within(drawer).getByRole("tab", { name: "执行日志" }));
  expect(within(drawer).queryByTestId("detail-log-pause")).toBeNull();
  expect(within(drawer).getByTestId("detail-log-maximize")).toBeTruthy();
  const historyLogLines = (within(drawer).getByTestId("detail-log").textContent ?? "").split("\n");
  expect(historyLogLines).toContain("line-0");
  expect(historyLogLines).toContain("line-1");
  expect(historyLogLines).toContain("line-500");
  expect(historyLogLines).toHaveLength(501);

  fireEvent.click(drawer.querySelector(".ant-drawer-close") as HTMLButtonElement);
  secondRow.focus();
  expect(fireEvent.keyDown(secondRow, { key: " " })).toBe(false);
  await screen.findByText("执行详情");
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
  await screen.findByText("执行详情");
  expect(screen.getByTestId("detail-input").textContent).toContain('"who": "B"');
  fireEvent.click(rows[0]);
  expect(screen.queryByTestId("detail-input")).toBeNull();

  // Click B again while A is still slow: B must win even though A resolves last.
  fireEvent.click(rows[1]);
  await screen.findByText("执行详情");
  expect(screen.getByTestId("detail-input").textContent).toContain('"who": "B"');

  releaseA();
  // Let A's late response settle, then verify the drawer still shows B.
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(screen.getByTestId("detail-input").textContent).toContain('"who": "B"');
  expect(screen.getByText("执行详情")).toBeTruthy();
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
  expect(within(offlineItem).getByText("离线")).toBeTruthy();
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
  ).toContain("运行节点离线");

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
  // M5.8-011：首页收敛为已确认的产品文案，不再堆叠功能列表。
  expect(screen.getByText("Code your data connections.")).toBeTruthy();
  expect(screen.getByText("把数据连接逻辑写成 Adapter（适配器），然后直接运行。")).toBeTruthy();
  expect(document.querySelector(".login-brand-intro")?.textContent).toBe(
    "从代码编辑、依赖配置到执行、日志与历史追踪，\nDataLinkRuntime 提供一个轻量、自托管的完整运行环境。",
  );
  expect(screen.getByText("Develop → Run → Observe")).toBeTruthy();
  expect(screen.getByText("© 2026 DataLinkRuntime · MIT License")).toBeTruthy();
  expect(screen.queryByText(/轻量 · 连接 · 适配 · 运行/)).toBeNull();
  expect(screen.queryByText("轻量易用")).toBeNull();
  expect(screen.queryByText("多元适配")).toBeNull();
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
  expect(screen.getByText("请选择一个适配器进行管理。")).toBeTruthy();
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
  type MediaListener = (event?: { matches: boolean }) => void;
  const listeners = new Set<MediaListener>();
  const media = { matches: false };
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      get matches() {
        return media.matches;
      },
      addEventListener: (_: string, listener: MediaListener) => {
        listeners.add(listener);
      },
      removeEventListener: (_: string, listener: MediaListener) => {
        listeners.delete(listener);
      },
      addListener: (listener: MediaListener) => {
        listeners.add(listener);
      },
      removeListener: (listener: MediaListener) => {
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
      listener({ matches: media.matches });
    }
  });
  await waitFor(() => {
    expect(monacoTheme()).toBe("vs-dark");
  });
});

it("keeps a manual dark Monaco theme across adapter switches and tab switches (M5.5.9)", async () => {
  localStorage.setItem(EDITOR_THEME_STORAGE_KEY, "dark");
  const versionA = makeVersion({ code: "code-a\n" });
  const versionB = makeVersion({ id: 20, adapter_id: 2, code: "code-b\n" });
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    {
      method: "GET",
      match: "/api/adapters",
      respond: () => ({
        body: [
          makeAdapter({ latest_version_id: 10 }),
          makeAdapter({ id: 2, name: "adapter-b", latest_version_id: 20 }),
        ],
      }),
    },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [{ id: 10, adapter_id: 1, seq: 1, created_at: "2026-08-15T00:00:00Z" }] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: versionA }) },
    { method: "GET", match: "/api/adapters/2/versions", respond: () => ({ body: [{ id: 20, adapter_id: 2, seq: 1, created_at: "2026-08-15T00:00:00Z" }] }) },
    { method: "GET", match: "/api/adapters/2/versions/20", respond: () => ({ body: versionB }) },
  ]);
  render(<App />);
  await selectFirstAdapter();
  expect(monacoTheme()).toBe("vs-dark");

  // 切换 Tab（运行设置 → 编辑）保持深色。
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  fireEvent.click(screen.getByRole("tab", { name: "编辑" }));
  expect(monacoTheme()).toBe("vs-dark");

  // 切换 Adapter（Monaco remount）保持深色。
  fireEvent.click(screen.getAllByTestId("adapter-item")[1]);
  await screen.findByRole("heading", { name: "adapter-b" });
  expect(monacoTheme()).toBe("vs-dark");
  expect(valueOf("code-editor")).toBe("code-b\n");
});

it("passes the active Monaco theme into the working-copy diff modal (M5.5.9)", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch(consoleWithVersionRoutes(adapter, makeVersion()));
  render(<App />);
  await selectFirstAdapter();
  expect(monacoTheme()).toBe("vs-dark");

  fireEvent.click(screen.getByTestId("working-diff"));
  await screen.findByTestId("version-diff");
  expect(screen.getByTestId("diff-editor").getAttribute("data-monaco-theme")).toBe("vs-dark");
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
  expect(within(screen.getByTestId("version-diff")).getByText("JavaScript 依赖")).toBeTruthy();
});

it("reopens the diff with the latest working copy content, never the previous session snapshot", async () => {
  // M5.5.6：Monaco model 缓存（固定 path + keepCurrent*Model）在真实浏览器中
  // 会在重新打开时命中旧 model；组件契约要求每次打开向 DiffEditor 传入
  // 当次会话的最新内容与语言（真实缓存行为另由浏览器证据覆盖）。
  const adapter = makeAdapter({ language: "python", latest_version_id: 10 });
  stubFetch(consoleWithVersionRoutes(adapter, makeVersion({ code: "base-v1\n" })));
  render(<App />);
  await selectFirstAdapter();

  // 第一次打开：内容 A
  fireEvent.change(screen.getByTestId("code-editor"), { target: { value: "first-session-code\n" } });
  fireEvent.click(screen.getByTestId("working-diff"));
  await screen.findByTestId("version-diff");
  expect(screen.getByTestId("diff-editor").getAttribute("data-modified")).toBe("first-session-code\n");

  // 关闭后编辑为内容 B，再打开：必须展示 B，而不是上一次会话的 A
  fireEvent.click(document.querySelector(".ant-modal-close") as Element);
  await waitFor(() => expect(screen.queryByTestId("version-diff")).toBeNull());
  fireEvent.change(screen.getByTestId("code-editor"), {
    target: { value: "second-session-code\n" },
  });
  fireEvent.click(screen.getByTestId("working-diff"));
  await screen.findByTestId("version-diff");
  expect(screen.getByTestId("diff-editor").getAttribute("data-original")).toBe("base-v1\n");
  expect(screen.getByTestId("diff-editor").getAttribute("data-modified")).toBe(
    "second-session-code\n",
  );
  expect(screen.getByTestId("diff-editor").getAttribute("data-monaco-language")).toBe("python");

  // 切 pane（依赖）再切回代码：内容保持当前会话，语言随 pane 变化
  const diffModal = screen.getByTestId("version-diff");
  fireEvent.click(within(diffModal).getByText("Python 依赖"));
  expect(screen.getByTestId("diff-editor").getAttribute("data-monaco-language")).toBe("plaintext");
  fireEvent.click(within(diffModal).getByText("代码"));
  expect(screen.getByTestId("diff-editor").getAttribute("data-modified")).toBe(
    "second-session-code\n",
  );
  expect(screen.getByTestId("diff-editor").getAttribute("data-monaco-language")).toBe("python");
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
  expect(screen.getByRole("textbox", { name: "绑定 1 代码中的凭据名" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "绑定 1 凭据" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "绑定 1 字段" })).toBeTruthy();

  // M5.5.7：凭据区域提供“去系统设置新建凭据”的说明与入口。
  expect(screen.getByText("如需新增凭据，请前往「系统设置 → 凭据管理」新建。")).toBeTruthy();
  const openSettings = screen.getByTestId("open-settings-for-credentials");
  expect(openSettings).toBeTruthy();

  fireEvent.click(screen.getByTestId("add-binding"));
  expect(screen.getByRole("group", { name: "绑定 2" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "绑定 2 代码中的凭据名" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "绑定 2 凭据" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "绑定 2 字段" })).toBeTruthy();
});

it("warns when the code-side credential name is changed", async () => {
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
  expect(screen.queryByTestId("binding-rename-hint")).toBeNull();
  fireEvent.change(screen.getByRole("textbox", { name: "绑定 1 代码中的凭据名" }), {
    target: { value: "DB_PASSWORD_NEW" },
  });
  expect(screen.getByTestId("binding-rename-hint").textContent).toContain(
    "修改代码中的凭据名后，代码中的引用也需要保持一致。",
  );
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
      method: "GET",
      match: "/api/package-sources/defaults",
      respond: () => ({
        body: {
          pypi: { kind: "pypi", name: "阿里云 PyPI 镜像", index_url: "https://mirrors.aliyun.com/pypi/simple/" },
          npm: { kind: "npm", name: "npmmirror npm 镜像", index_url: "https://registry.npmmirror.com/" },
          maven: { kind: "maven", name: "阿里云 Maven 公共仓库", index_url: "https://maven.aliyun.com/repository/public" },
        },
      }),
    },
    {
      method: "POST",
      match: "/api/package-sources/1/test",
      respond: () => ({
        body: { ok: false, status_code: null, error: "Connection refused" },
      }),
    },
  ]);
  render(<App />);
  fireEvent.click(await screen.findByTestId("user-menu"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "系统设置" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "凭据" }));

  // 凭据中心只展示元数据，API 不会回传明文。
  await screen.findByTestId("credentials-panel");
  await screen.findByTestId("credential-row");
  expect(screen.getByTestId("credential-row").textContent).toBe("db-password");
  fireEvent.click(screen.getByTestId("new-credential"));
  expect(screen.getByRole("textbox", { name: "凭据名称" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "凭据类型" })).toBeTruthy();
  expect(screen.getByLabelText("凭据字段 username")).toBeTruthy();
  expect(screen.getByLabelText("凭据字段 password")).toBeTruthy();
  // 依赖源页签：默认源标记 + 可达性测试 + 恢复默认入口。
  fireEvent.click(screen.getByText("依赖源"));
  await screen.findByTestId("package-source-row");
  expect(screen.getByTestId("default-source-badge")).toBeTruthy();
  // PyPI 已有默认源时不显示清空回退提示；npm / Maven 无默认源时显示明确回退。
  expect(screen.queryByTestId("no-default-source-pypi")).toBeNull();
  expect(screen.getByTestId("no-default-source-npm").textContent).toContain("不会使用未配置地址");
  fireEvent.click(screen.getByTestId("restore-default-menu"));
  expect(screen.getByTestId("restore-default-pypi")).toBeTruthy();
  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.click(screen.getByTestId("new-package-source"));
  expect(screen.getByRole("textbox", { name: "依赖源名称" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "依赖源类型" })).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "索引 URL" })).toBeTruthy();
  expect(screen.getByRole("combobox", { name: "依赖源凭据" })).toBeTruthy();

  fireEvent.click(screen.getByTestId("test-package-source"));
  await screen.findByTestId("package-source-test-result");
  expect(screen.getByTestId("package-source-test-result").textContent).toBe(
    "不可达",
  );
  expect(screen.getByTestId("package-source-test-result").getAttribute("role")).toBe("alert");
  expect(document.body.textContent).not.toContain("Connection refused");
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

// M5.7 Wave B3: stable B2 attachment limits/MIME/capability contract.
function aiAttachmentCapabilitiesRoute(overrides: Partial<AiAttachmentCapabilities> = {}): Route {
  return {
    method: "GET",
    match: "/api/ai/attachment-capabilities",
    respond: () => ({
      body: {
        limits: {
          max_attachments: 8,
          max_file_bytes: 6 * 1024 * 1024,
          max_total_bytes: 12 * 1024 * 1024,
          max_parsed_chars_per_file: 64 * 1024,
          max_parsed_total_chars: 256 * 1024,
          parse_timeout_seconds: 30,
        },
        supported_content_types: [
          "application/json",
          "application/octet-stream",
          "application/pdf",
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          "application/x-yaml",
          "application/xml",
          "application/javascript",
          "image/jpeg",
          "image/png",
          "image/webp",
          "text/csv",
          "text/javascript",
          "text/markdown",
          "text/plain",
          "text/x-yaml",
          "text/xml",
        ],
        providers: [
          { provider: "openai", images_native: true, files_native: false },
          { provider: "deepseek", images_native: false, files_native: false },
          { provider: "kimi", images_native: false, files_native: false },
          { provider: "minimax", images_native: false, files_native: false },
          { provider: "custom_openai_compatible", images_native: false, files_native: false },
        ],
        ...overrides,
      },
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

it.each(["python", "javascript", "java"] as const)(
  "M5.8-003 keeps %s Candidate Diff/Apply code-only across languages",
  async (language) => {
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
      aiAttachmentCapabilitiesRoute(),
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

    // M5.5.13：选区快照随请求发送，且不改变 Candidate → Diff → Apply 路径。
    selectInEditor(`selected-${language}`, 2, 3);
    fireEvent.click(screen.getByTestId("add-ai-selection"));
    expect(screen.getByTestId("ai-snippet-label").textContent).toContain("第 2–3 行");
    expect(screen.getByTestId("ai-snippet-label").textContent).toContain("代码");

    fireEvent.change(screen.getByTestId("ai-message-input"), {
      target: { value: "增加分页" },
    });
    fireEvent.click(screen.getByTestId("ai-send"));

    await screen.findByTestId("ai-candidate-summary");
    expect(screen.getByTestId("ai-candidate-summary").textContent).toBe("增加分页处理");
    // M5.5.4：聊天卡片收敛为单一路径“代码已生成 + [查看修改]”，
    // 不再同时堆“查看修改 / 应用 / 放弃”。
    expect(screen.getByTestId("ai-candidate-ready").textContent).toBe("代码已生成");
    expect(screen.getByTestId("ai-required-secret-keys").textContent).toContain("API_TOKEN");
    expect(screen.getByTestId("ai-missing-secret-keys").textContent).toContain("MISSING_TOKEN");
    expect(screen.getByTestId("ai-missing-secret-keys").textContent).not.toContain(
      "：API_TOKEN,",
    );
    expect(screen.getByTestId("ai-view-diff").textContent).toContain("查看修改");
    expect(screen.queryByTestId("ai-apply-candidate")).toBeNull();
    expect(screen.queryByTestId("ai-discard-candidate")).toBeNull();

    const payload = JSON.parse(assistBody) as {
      message: string;
      working_copy: {
        code: string;
        requirements: string;
        runtime_config: Record<string, unknown>;
      };
      recent_messages: unknown[];
      base_version_id: number;
      context_snippets: {
        source: string;
        text: string;
        start_line: number;
        end_line: number;
      }[];
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
      context_snippets: [
        {
          source: "code",
          text: `selected-${language}`,
          start_line: 2,
          end_line: 3,
        },
      ],
    });

    fireEvent.click(screen.getByTestId("ai-view-diff"));
    const diffModal = await screen.findByTestId("version-diff");
    expect(screen.getByTestId("diff-editor").getAttribute("data-monaco-language")).toBe(language);
    expect(within(diffModal).getByText("代码")).toBeTruthy();
    expect(within(diffModal).queryByText(/依赖/)).toBeNull();
    expect(within(diffModal).queryByText(/运行参数/)).toBeNull();

    // Apply 只发生在 Diff 内（M5.5.4 单一路径）；成功后 Diff 自动关闭
    // （M5.5.13），返回 Monaco/Workbench。
    fireEvent.click(screen.getByTestId("diff-apply-candidate"));
    await waitFor(() => expect(screen.queryByTestId("version-diff")).toBeNull());
    expect(valueOf("code-editor")).toBe("candidate-code\n");
    expect(screen.getByTestId("ai-candidate-applied")).toBeTruthy();
    expect(valueOf("requirements-input")).toBe(`base-dependency-${language}\n`);
    // M5.8-003：运行参数（JSON）仍由人工 Working Copy 管理，Candidate 不会覆盖它。
    expect(screen.queryByText("运行参数（JSON）")).toBeNull();
    // 候选已应用 = 工作副本变 dirty：运行按钮被“请先保存当前修改”门禁拦截。
    await waitFor(() => {
      expect((screen.getByTestId("header-task-run-once") as HTMLButtonElement).disabled).toBe(true);
    });
    expect(
      (screen.getByTestId("header-task-run-once") as HTMLButtonElement)
        .closest(".action-with-reason")
        ?.getAttribute("aria-label"),
    ).toContain("请先保存当前修改，再运行。");

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
  expect(screen.queryByTestId("ai-apply-candidate")).toBeNull();
  expect(screen.queryByTestId("ai-discard-candidate")).toBeNull();
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
  // M5.5.4：stale Candidate 走同一条 查看修改 → Diff → 仍然应用 路径。
  fireEvent.click(screen.getByTestId("ai-view-diff"));
  await screen.findByTestId("version-diff");
  expect(screen.getByTestId("diff-candidate-stale")).toBeTruthy();
  expect(screen.getByTestId("diff-apply-candidate").textContent).toContain("仍然应用");

  fireEvent.click(screen.getByTestId("diff-apply-candidate"));
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
    aiAttachmentCapabilitiesRoute(),
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

// --- M5.5.4 回归：悬浮入口 / Candidate → Diff → Apply 单一路径 -------------

it("defaults to the floating entry that does not compress Monaco, and expands/collapses it", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    aiBindingsRoute(1),
  ]);
  render(<App />);
  await selectFirstAdapter();

  // 默认只显示悬浮入口：不渲染面板。
  const entry = await screen.findByTestId("open-ai-assistant");
  expect(screen.queryByTestId("ai-assistant-panel")).toBeNull();
  // 收起态必须绝对定位、脱离 flex 布局，隐藏的助手才不会压缩 Monaco 主编辑区；
  // 展开态保留在布局流中（flex: 0 0 auto），因此不遮挡保存/运行按钮。
  const appStyles = readFileSync(join(process.cwd(), "src/index.css"), "utf8");
  expect(appStyles).toMatch(/\.ai-assistant-collapsed\s*\{[^}]*position\s*:\s*absolute/s);
  expect(appStyles).toMatch(/\.ai-assistant-collapsed\s*\{[^}]*right\s*:\s*16px/s);
  expect(appStyles).toMatch(/\.ai-assistant-expanded\s*\{[^}]*flex\s*:\s*0\s+0\s+auto/s);
  expect(entry.tagName).toBe("BUTTON");

  // 展开：悬浮入口消失，面板回到布局流中。
  fireEvent.click(entry);
  await screen.findByTestId("ai-assistant-panel");
  expect(screen.queryByTestId("open-ai-assistant")).toBeNull();
  expect(document.querySelector(".ai-assistant-expanded")).not.toBeNull();

  // 收起：回到悬浮入口。
  fireEvent.click(screen.getByTestId("close-ai-assistant"));
  await screen.findByTestId("open-ai-assistant");
  expect(screen.queryByTestId("ai-assistant-panel")).toBeNull();
});

it("closing the Candidate Diff applies nothing and keeps the Working Copy untouched", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion({ code: "base-code\n" })),
    aiBindingsRoute(1),
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: () => ({ body: aiResponse("候选已生成", AI_CANDIDATE) }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "改代码" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-candidate-summary");

  fireEvent.click(screen.getByTestId("ai-view-diff"));
  await screen.findByTestId("version-diff");
  // 关闭 Diff = 不应用，不需要额外的“放弃”动作。
  fireEvent.click(screen.getByTestId("diff-close"));
  await waitFor(() => expect(screen.queryByTestId("version-diff")).toBeNull());

  expect(valueOf("code-editor")).toBe("base-code\n");
  // 关闭 Diff = 未应用：工作副本保持干净，运行门禁不拦截。
  await waitFor(() => {
    expect((screen.getByTestId("header-task-run-once") as HTMLButtonElement).disabled).toBe(false);
  });
  expect(
    (screen.getByTestId("header-task-run-once") as HTMLButtonElement)
      .closest(".action-with-reason")
      ?.getAttribute("aria-label") ?? "",
  ).not.toContain("请先保存当前修改");
  expect(screen.queryByTestId("ai-candidate-applied")).toBeNull();
  // 候选仍在卡片上可再次审阅。
  expect(screen.getByTestId("ai-view-diff")).toBeTruthy();
});

it("never lets an invalid Candidate be reviewed or applied", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion({ code: "base-code\n" })),
    aiBindingsRoute(1),
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: () => ({
        body: aiResponse("候选已生成", {
          summary: "坏候选",
          code: "   ",
          requirements: "x",
          runtime_config: {},
          required_secret_keys: [],
        }),
      }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "改代码" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));

  // 助手消息仍然展示，但形状不符的 Candidate 被整体丢弃：
  // 没有候选卡片、没有 查看修改、没有可应用的入口。
  await screen.findByText("候选已生成");
  expect(screen.queryByTestId("ai-candidate")).toBeNull();
  expect(screen.queryByTestId("ai-view-diff")).toBeNull();
  expect(screen.queryByTestId("diff-apply-candidate")).toBeNull();
  expect(valueOf("code-editor")).toBe("base-code\n");
});

it("never surfaces Secret values in the UI or in the Assist request", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const secretValue = "sk-test-super-secret-value-9f3a";
  const requestBodies: string[] = [];
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: "/api/adapters/1/credential-bindings",
      respond: () => ({
        body: [{ env_key: "API_TOKEN", credential_id: 1, field: "token" }],
      }),
    },
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: (body) => {
        requestBodies.push(body ?? "");
        return { body: aiResponse("候选已生成", AI_CANDIDATE) };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "分页" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-candidate-summary");

  // 浏览器请求只携带工作副本与对话，绝不携带绑定名称或 Secret 真值；
  // UI 只展示 env_key 名称，夹具中的 Secret 真值永不出现。
  expect(requestBodies).toHaveLength(1);
  expect(requestBodies[0]).not.toContain("API_TOKEN");
  expect(requestBodies[0]).not.toContain("MISSING_TOKEN");
  expect(requestBodies[0]).not.toContain(secretValue);
  expect(document.body.textContent).toContain("API_TOKEN");
  expect(document.body.textContent).not.toContain(secretValue);
  expect(document.body.textContent).not.toContain("Bearer ");
});

it("keeps soft-deleted Adapters out of the active Catalog without Archive/Restore UI", async () => {
  const deleted = makeAdapter({ archived_at: "2026-08-11T01:00:00Z" });
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [deleted] }) },
  ]);
  render(<App />);
  await screen.findByTestId("adapter-catalog");
  expect(screen.queryByTestId("adapter-item")).toBeNull();
  expect(screen.queryByText("已归档")).toBeNull();
  expect(screen.queryByText(/恢复 Adapter/)).toBeNull();
  expect(screen.getByText("暂无适配器")).toBeTruthy();
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
  fireEvent.click(await screen.findByTestId("user-menu"));
  fireEvent.click(await screen.findByRole("menuitem", { name: "系统设置" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: "AI 模型" }));
  await screen.findByTestId("ai-model-settings-panel");

  expect(screen.getByTestId("ai-data-boundary-warning").textContent).toContain("数据范围");
  expect(screen.getByTestId("ai-data-boundary-warning").textContent).toContain(
    "Secret 不会发送",
  );
  fireEvent.click(screen.getByText("推理设置：跟随模型默认"));
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
  const modelInput = screen.getByTestId("ai-model-input");
  fireEvent.change(modelInput, { target: { value: "" } });
  fireEvent.focus(modelInput);
  fireEvent.mouseDown(modelInput);
  let modelOption: HTMLElement | undefined;
  await waitFor(() => {
    modelOption = screen
      .getAllByText("model-from-server")
      .find((element) => element.classList.contains("ant-select-item-option-content"));
    expect(modelOption).toBeDefined();
  });
  if (modelOption === undefined) {
    throw new Error("AutoComplete model option was not rendered");
  }
  fireEvent.click(modelOption.closest(".ant-select-item-option") ?? modelOption);
  expect(valueOf("ai-model-input")).toBe("model-from-server");
  fireEvent.change(modelInput, { target: { value: "manual-model" } });

  fireEvent.click(screen.getByTestId("ai-test-connection"));
  await waitFor(() => expect(testBody).not.toBe(""));
  expect(screen.getByTestId("ai-settings-notice").textContent).toContain("连接测试通过");

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

// --- M5.4.3 Webhook 适配器 final user model ---------------------------------

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

it("shows the Webhook starter and only 编辑 / 运行设置 / 调用记录 / 实时日志", async () => {
  const adapter = makeAdapter({ adapter_type: "webhook", name: "hook-a", runtime_worker_id: 3 });
  stubFetch(webhookConsoleRoutes(adapter));
  render(<App />);
  await selectFirstAdapter();

  expect(valueOf("code-editor")).toBe(WEBHOOK_STARTER_CODE);
  expect(screen.getByTestId("webhook-workbench-header")).toBeDefined();
  expect(screen.getByRole("tab", { name: "编辑" })).toBeDefined();
  expect(screen.getByRole("tab", { name: "运行设置" })).toBeDefined();
  expect(screen.getByRole("tab", { name: "调用记录" })).toBeDefined();
  expect(screen.getByRole("tab", { name: "实时日志" })).toBeDefined();
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
  // M5.5.10：只展示本次 Webhook 调用（内部 ID 不在用户界面出现）。
  expect(document.body.textContent).toContain("Webhook 触发");
  expect(document.body.textContent).not.toContain("主动触发");
  expect(document.body.textContent).not.toMatch(/调用 #/);
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
        adapter = { ...adapter, ...JSON.parse(body ?? "{}") };
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

  // M5.5.12：URL 只展示一次——完整地址只读 + 复制，不再拆成前缀 + 路径双框。
  expect(screen.queryByTestId("webhook-prefix")).toBeNull();
  const fullUrlInput = screen.getByTestId("webhook-url") as HTMLInputElement;
  expect(fullUrlInput.readOnly).toBe(true);
  fireEvent.change(fullUrlInput, { target: { value: "https://attacker.invalid/changed" } });
  expect(fullUrlInput.value).toBe(`${window.location.origin}/api/hooks/a8f3c9d2`);
  expect(screen.getByTestId("webhook-url-readonly").className).toContain("webhook-url-control");
  expect(screen.getByTestId("webhook-url").getAttribute("aria-label")).toBe("完整地址（只读）");
  expect(screen.getByTestId("webhook-url")).toHaveProperty(
    "value",
    `${window.location.origin}/api/hooks/a8f3c9d2`,
  );
  // 运行设置只保留五类字段，无“接收状态”输入、无页面内启停按钮、无手工刷新。
  expect(screen.getByText("Webhook 路径")).toBeDefined();
  expect(screen.getByText("完整地址")).toBeDefined();
  expect(screen.getByText("入口调用鉴权（Bearer Token）")).toBeDefined();
  expect(screen.getByText("运行节点")).toBeDefined();
  expect(screen.getByText("单次执行超时（一次运行的最长时间）")).toBeDefined();
  expect(document.body.textContent).toContain("一次调用超过该时间后，系统将自动结束并标记为“超时”。");
  expect(document.body.textContent).not.toContain("此配置不是：");
  expect(screen.queryByText("接收状态")).toBeNull();
  expect(screen.queryByTestId("webhook-start")).toBeNull();
  expect(screen.queryByTestId("webhook-stop")).toBeNull();
  expect(screen.queryByText("刷新")).toBeNull();

  fireEvent.change(screen.getByTestId("webhook-public-id"), { target: { value: "receive-sys1-data" } });
  fireEvent.click(screen.getByTestId("webhook-save"));
  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
      ),
    ).toBe(true);
  });
  // M5.5.11：保存运行配置同时下发 Adapter 级单次执行超时（默认 300 秒）。
  const patchCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
  );
  expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
    runtime_worker_id: 3,
    timeout_seconds: 300,
  });
  // 开启接收只保留在 Header 右上角。
  fireEvent.click(screen.getByTestId("header-webhook-toggle"));
  await waitFor(() => expect(screen.getByTestId("header-webhook-toggle").textContent).toContain("停止接收"));
  expect(screen.getByTestId("live-log-workspace").textContent).toContain("等待 Webhook 请求…");

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
  let webhook = makeWebhook();
  const fetchMock = stubFetch([
    ...webhookConsoleRoutes(adapter, webhook),
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: () => ({ body: adapter }),
    },
    {
      method: "PUT",
      match: "/api/adapters/1/webhook",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}");
        if (payload.enabled) {
          return {
            status: 409,
            body: { detail: { code: "webhook_path_in_use", message: "conflict" } },
          };
        }
        webhook = { ...webhook, ...payload, hook_path: `/api/hooks/${payload.public_id}` };
        return { body: webhook };
      },
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

  // 未保存修改时 Header 的“开启接收”也必须被禁用；保存后才允许开始接收。
  fireEvent.change(screen.getByTestId("webhook-public-id"), { target: { value: "receive-sys1-data" } });
  fireEvent.click(screen.getByTestId("webhook-save"));
  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT",
      ),
    ).toBe(true);
  });
  fireEvent.click(screen.getByTestId("header-webhook-toggle"));
  expect((await screen.findByRole("alert")).textContent).toContain(
    "Webhook 地址 receive-sys1-data 当前正在被另一个运行中的适配器使用",
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
  fireEvent.click(screen.getByTestId("header-webhook-toggle"));
  await waitFor(() => expect(screen.getByTestId("header-webhook-toggle").textContent).toContain("停止接收"));
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
  // 接收中：配置锁定但信息可读（锁图标 + 文本，不隐藏、不灰到不可读），
  // 运行设置内不再有第二块黄色锁定 Alert。
  expect(screen.getByTestId("webhook-path-locked").textContent).toContain("a8f3c9d2");
  expect(screen.getByTestId("webhook-credential-locked").textContent).toContain("hook-token");
  expect(screen.getByTestId("webhook-worker-locked").textContent).toContain("hook-worker");
  expect(screen.getByTestId("webhook-timeout-locked").textContent).toContain("5 分钟");
  expect(screen.queryByTestId("webhook-runtime-locked")).toBeNull();
  expect(document.body.textContent).not.toContain("real-hook-secret");
  // 完整 Webhook 地址运行中仍允许复制。
  expect((screen.getByTestId("webhook-copy") as HTMLButtonElement).disabled).toBe(false);
  expect(screen.getByTestId("webhook-url-readonly").className).toContain("webhook-url-control");

  // 有 active Webhook Execution：点击“停止接收”必须弹出三选一。
  fireEvent.click(screen.getByTestId("header-webhook-toggle"));
  expect(await screen.findByTestId("webhook-stop-dialog-text")).toBeDefined();
  expect(screen.getByTestId("webhook-stop-end").textContent).toBe("直接结束当前调用");
  expect(screen.getByTestId("webhook-stop-wait").textContent).toBe("等待调用结束");
  expect(screen.getByTestId("webhook-stop-cancel").textContent?.replace(/\s/g, "")).toBe("取消");

  // “等待调用结束”：立即停止接收新请求，不取消当前调用，配置锁保持。
  fireEvent.click(screen.getByTestId("webhook-stop-wait"));
  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT",
      ),
    ).toBe(true);
  });
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/executions/91/cancel" && init?.method === "POST",
    ),
  ).toBe(false);
  const stopCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT",
  );
  expect(JSON.parse(String(stopCall?.[1]?.body))).toEqual({
    enabled: false,
    public_id: "a8f3c9d2",
    credential_id: 7,
  });
  // 已停止接收，但当前调用仍在执行：只保留一行低干扰锁定提示。
  expect(screen.getByTestId("webhook-path-locked")).toBeDefined();
  await waitFor(() =>
    expect(screen.getByTestId("webhook-active-execution").textContent).toContain(
      "当前调用仍在执行，运行配置已锁定",
    ),
  );
});

it("stops receiving directly without a dialog when there is no active call", async () => {
  const adapter = makeAdapter({
    adapter_type: "webhook",
    latest_version_id: 10,
    runtime_worker_id: 3,
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
  fireEvent.click(screen.getByTestId("header-webhook-toggle"));
  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT",
      ),
    ).toBe(true);
  });
  // 无 active 调用：直接停止，不弹选择框。
  expect(screen.queryByTestId("webhook-stop-dialog-text")).toBeNull();
  const stopCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT",
  );
  expect(JSON.parse(String(stopCall?.[1]?.body)).enabled).toBe(false);
  expect(screen.getByTestId("header-webhook-toggle").textContent).toBe("开启接收");
});

it("ends the active call immediately when the user chooses 直接结束当前调用", async () => {
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
    {
      method: "POST",
      match: "/api/executions/91/cancel",
      respond: () => ({
        body: {
          id: 91,
          adapter_id: 1,
          version_id: 10,
          worker_id: 3,
          target_worker_id: 3,
          trigger: "webhook",
          scheduled_for: null,
          status: "cancelled",
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
          created_at: "2026-08-15T00:00:00Z",
          started_at: "2026-08-15T00:00:00Z",
          ended_at: "2026-08-15T00:00:00Z",
          duration_ms: 1200,
        },
      }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  await screen.findByTestId("webhook-run-settings");
  fireEvent.click(screen.getByTestId("header-webhook-toggle"));
  await screen.findByTestId("webhook-stop-dialog-text");
  fireEvent.click(screen.getByTestId("webhook-stop-end"));

  // “直接结束”：立即停止接收，并复用已有 Execution cancel 机制。
  await screen.findByText("已停止接收，当前调用已结束。");
  const stopCalls = fetchMock.mock.calls.filter(
    ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT",
  );
  expect(stopCalls.length).toBe(1);
  expect(JSON.parse(String(stopCalls[0]?.[1]?.body))).toEqual({
    enabled: false,
    public_id: "a8f3c9d2",
    credential_id: 7,
  });
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/executions/91/cancel" && init?.method === "POST",
    ),
  ).toBe(true);
  // 绝无任何重新开启接收的请求。
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) =>
        String(url) === "/api/adapters/1/webhook" &&
        init?.method === "PUT" &&
        JSON.parse(String(init?.body)).enabled === true,
    ),
  ).toBe(false);
});

it("keeps receiving and the active call when the user cancels the dialog", async () => {
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
  fireEvent.click(screen.getByTestId("header-webhook-toggle"));
  await screen.findByTestId("webhook-stop-dialog-text");
  fireEvent.click(screen.getByTestId("webhook-stop-cancel"));

  // 取消：保持接收状态和 active Execution 不变，不发任何启停请求。
  expect(screen.queryByTestId("webhook-stop-dialog-text")).toBeNull();
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT",
    ),
  ).toBe(false);
  expect(screen.getByTestId("header-webhook-toggle").textContent).toBe("停止接收");
});

it("keeps the true stopped state with an actionable error when cancel fails", async () => {
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
    {
      method: "POST",
      match: "/api/executions/91/cancel",
      respond: () => ({
        status: 500,
        body: { detail: { code: "internal_error", message: "cancel exploded" } },
      }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  await screen.findByTestId("webhook-run-settings");
  fireEvent.click(screen.getByTestId("header-webhook-toggle"));
  await screen.findByTestId("webhook-stop-dialog-text");
  fireEvent.click(screen.getByTestId("webhook-stop-end"));

  // cancel 失败：状态必须真实（已停止接收、调用仍在执行），绝不偷偷重新开启。
  const banner = await screen.findByTestId("error-banner");
  expect(banner.textContent).toContain("已停止接收，但取消当前调用失败");
  expect(banner.textContent).toContain("当前调用仍在执行");
  const webhookPuts = fetchMock.mock.calls.filter(
    ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT",
  );
  expect(webhookPuts.length).toBe(1);
  expect(JSON.parse(String(webhookPuts[0]?.[1]?.body)).enabled).toBe(false);
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT" &&
        JSON.parse(String(init?.body)).enabled === true,
    ),
  ).toBe(false);
  // 锁定字段仍可读，Header 只保留一行低干扰提示。
  expect(screen.getByTestId("webhook-path-locked").textContent).toContain("a8f3c9d2");
  await waitFor(() =>
    expect(screen.getByTestId("webhook-active-execution").textContent).toContain(
      "当前调用仍在执行，运行配置已锁定",
    ),
  );
});

it("blocks receiving while runtime settings have unsaved changes", async () => {
  const adapter = makeAdapter({
    adapter_type: "webhook",
    latest_version_id: 10,
    runtime_worker_id: 3,
  });
  const fetchMock = stubFetch([
    ...webhookConsoleRoutes(adapter),
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: () => ({ body: adapter }),
    },
    {
      method: "PUT",
      match: "/api/adapters/1/webhook",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}");
        return { body: { ...makeWebhook(), ...payload, hook_path: `/api/hooks/${payload.public_id}` } };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  await screen.findByTestId("webhook-run-settings");
  fireEvent.change(screen.getByTestId("webhook-public-id"), { target: { value: "receive-sys1-data" } });
  const startButton = screen.getByTestId("header-webhook-toggle") as HTMLButtonElement;
  expect(startButton.disabled).toBe(true);
  expect(startButton.closest(".action-with-reason")?.getAttribute("aria-label")).toContain(
    "运行设置有未保存修改，请先保存",
  );
  // 保存运行配置后未保存门禁解除，Header 才能开启接收。
  fireEvent.click(screen.getByTestId("webhook-save"));
  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => String(url) === "/api/adapters/1/webhook" && init?.method === "PUT",
      ),
    ).toBe(true);
  });
  expect((screen.getByTestId("header-webhook-toggle") as HTMLButtonElement).disabled).toBe(false);
});

it("saves the Adapter-level single-run timeout from Webhook run settings (M5.5.11)", async () => {
  let adapter = makeAdapter({
    adapter_type: "webhook",
    latest_version_id: 10,
    runtime_worker_id: 3,
  });
  const fetchMock = stubFetch([
    ...webhookConsoleRoutes(adapter),
    {
      method: "PATCH",
      match: "/api/adapters/1",
      respond: (body) => {
        adapter = { ...adapter, ...JSON.parse(body ?? "{}") };
        return { body: adapter };
      },
    },
    {
      method: "PUT",
      match: "/api/adapters/1/webhook",
      respond: (body) => {
        const payload = JSON.parse(body ?? "{}");
        return { body: { ...makeWebhook(), ...payload, hook_path: `/api/hooks/${payload.public_id}` } };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  fireEvent.click(screen.getByRole("tab", { name: "运行设置" }));
  await screen.findByTestId("webhook-run-settings");
  fireEvent.click(screen.getByLabelText("10 分钟"));
  fireEvent.click(screen.getByTestId("webhook-save"));
  await waitFor(() => {
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
      ),
    ).toBe(true);
  });
  const patchCall = fetchMock.mock.calls.find(
    ([url, init]) => String(url) === "/api/adapters/1" && init?.method === "PATCH",
  );
  expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
    runtime_worker_id: 3,
    timeout_seconds: 600,
  });
});

it("keeps Webhook receive disabled until the active call reaches a terminal state", () => {
  const adapter = makeAdapter({
    adapter_type: "webhook",
    latest_version_id: 10,
    runtime_worker_id: 3,
    runtime_locked: true,
    running_execution_id: 91,
  });
  const commonProps = {
    adapter,
    runtimeWorker: null,
    busy: false,
    contentReady: true,
    onSave: vi.fn(),
    onOpenSettings: vi.fn(),
    onToggleReceiving: vi.fn(),
  };
  const { rerender } = render(
    <WebhookWorkbenchHeader
      {...commonProps}
      runtimeState={{ loaded: true, enabled: true, runtimeLocked: true, changingState: false, startBlockedReason: null }}
    />,
  );
  expect(screen.getByTestId("header-webhook-toggle").textContent).toBe("停止接收");

  rerender(
    <WebhookWorkbenchHeader
      {...commonProps}
      runtimeState={{ loaded: true, enabled: false, runtimeLocked: true, changingState: false, startBlockedReason: null }}
    />,
  );
  const lockedStart = screen.getByTestId("header-webhook-toggle") as HTMLButtonElement;
  expect(lockedStart.textContent).toBe("开启接收");
  expect(lockedStart.disabled).toBe(true);
  expect(lockedStart.closest(".action-with-reason")?.getAttribute("aria-label")).toContain(
    "已有调用仍在运行，请等待其进入终态后再开启接收或修改运行配置",
  );

  rerender(
    <WebhookWorkbenchHeader
      {...commonProps}
      adapter={{ ...adapter, runtime_locked: false, running_execution_id: null }}
      runtimeState={{ loaded: true, enabled: false, runtimeLocked: false, changingState: false, startBlockedReason: null }}
    />,
  );
  expect((screen.getByTestId("header-webhook-toggle") as HTMLButtonElement).disabled).toBe(false);
});

// --- M5.5.5：Monaco 选区上下文与平台阶段进度 --------------------------------

function selectInEditor(text: string, startLine: number, endLine: number) {
  act(() => {
    monacoHarness.setText(text);
    monacoHarness.setSelection({
      startLineNumber: startLine,
      startColumn: 1,
      endLineNumber: endLine,
      endColumn: 2,
    });
  });
}

it("adds the exact Monaco selection snapshot, auto-opens the panel, and sends it unchanged", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const requestBodies: string[] = [];
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    aiBindingsRoute(1),
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: (body) => {
        requestBodies.push(body ?? "");
        return { body: aiResponse("已生成修改候选", AI_CANDIDATE) };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();

  // 空选区：入口禁用，不提供无意义操作。
  const addButton = screen.getByTestId("add-ai-selection") as HTMLButtonElement;
  expect(addButton.disabled).toBe(true);
  expect(screen.queryByTestId("ai-context-snippets")).toBeNull();

  // 非空选区：入口可用；点击后出现行号标记，并自动展开 AI 面板（M5.5.13）。
  selectInEditor("def handle(context, input):\n    return input\n", 2, 3);
  expect(addButton.disabled).toBe(false);
  fireEvent.click(addButton);
  await screen.findByTestId("ai-assistant-panel");
  expect(screen.getByTestId("ai-snippet-label").textContent).toBe("代码 第 2–3 行（Python）");

  // 光标随后移动（选区收起）：按钮回到禁用，但已确认的快照不受影响。
  act(() => {
    monacoHarness.setSelection({
      startLineNumber: 2,
      startColumn: 1,
      endLineNumber: 2,
      endColumn: 1,
    });
  });
  expect(addButton.disabled).toBe(true);
  expect(screen.getByTestId("ai-snippet-label").textContent).toContain("第 2–3 行");

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "解释选中代码" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-candidate-summary");

  const payload = JSON.parse(requestBodies[0]) as {
    context_snippets: { source: string; text: string; start_line: number; end_line: number }[];
  };
  expect(payload.context_snippets).toEqual([
    {
      source: "code",
      text: "def handle(context, input):\n    return input\n",
      start_line: 2,
      end_line: 3,
    },
  ]);
});

it("rejects whitespace-only selections and supports single delete and clear-all", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  const requestBodies: string[] = [];
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    aiBindingsRoute(1),
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: (body) => {
        requestBodies.push(body ?? "");
        return { body: aiResponse("候选已生成", AI_CANDIDATE) };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();
  const addButton = screen.getByTestId("add-ai-selection") as HTMLButtonElement;

  // 纯空白选区不产生上下文（文本必须非空才有意义）：入口可用但点击被拒绝。
  act(() => {
    monacoHarness.setText("   ");
    monacoHarness.setSelection({
      startLineNumber: 1,
      startColumn: 1,
      endLineNumber: 1,
      endColumn: 4,
    });
  });
  expect(addButton.disabled).toBe(false);
  fireEvent.click(addButton);
  expect(screen.queryByTestId("ai-context-snippets")).toBeNull();

  // 加入 → 单独删除：标记消失，发送时不携带 context_snippets。
  selectInEditor("first selection\n", 1, 1);
  fireEvent.click(screen.getByTestId("add-ai-selection"));
  expect(screen.getByTestId("ai-snippet-label").textContent).toContain("第 1 行");
  fireEvent.click(screen.getByTestId("ai-remove-snippet-1"));
  expect(screen.queryByTestId("ai-context-snippets")).toBeNull();

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "不带片段" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-candidate-summary");
  const clearedPayload = JSON.parse(requestBodies[0]) as {
    context_snippets?: unknown;
  };
  expect(clearedPayload.context_snippets).toBeUndefined();

  // 追加：新片段不覆盖旧片段，按加入顺序展示（M5.5.13）。
  selectInEditor("first snippet\n", 1, 1);
  fireEvent.click(screen.getByTestId("add-ai-selection"));
  selectInEditor("second snippet\n", 4, 5);
  fireEvent.click(screen.getByTestId("add-ai-selection"));
  const labels = screen.getAllByTestId("ai-snippet-label").map((node) => node.textContent);
  expect(labels[0]).toContain("第 1 行");
  expect(labels[1]).toContain("第 4–5 行");

  // 删除第一段：第二段保持；发送只携带剩余片段（加入顺序不变）。
  fireEvent.click(screen.getByTestId("ai-remove-snippet-2"));
  expect(screen.getAllByTestId("ai-snippet-label")).toHaveLength(1);
  expect(screen.getByTestId("ai-snippet-label").textContent).toContain("第 4–5 行");

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "带第二段" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await waitFor(() => expect(requestBodies).toHaveLength(2));
  const secondPayload = JSON.parse(requestBodies[1]) as {
    context_snippets: { source: string; text: string; start_line: number; end_line: number }[];
  };
  expect(secondPayload.context_snippets).toEqual([
    { source: "code", text: "second snippet\n", start_line: 4, end_line: 5 },
  ]);

  // M5.8-002：请求进入发送后，本轮片段自动从待发送区消失；历史消息仍保留。
  expect(screen.queryByTestId("ai-context-snippets")).toBeNull();

  // 清空全部仍可用于用户手工移除尚未发送的新片段。
  selectInEditor("third snippet\n", 6, 6);
  fireEvent.click(screen.getByTestId("add-ai-selection"));
  expect(screen.getByTestId("ai-context-snippets")).toBeTruthy();
  fireEvent.click(screen.getByTestId("ai-clear-all-snippets"));
  expect(screen.queryByTestId("ai-context-snippets")).toBeNull();
});

it("clears the context snippets on Adapter switch and never cross-talks", async () => {
  const adapterA = makeAdapter({ id: 1, name: "adapter-a" });
  const adapterB = makeAdapter({ id: 2, name: "adapter-b" });
  const requestBodies: string[] = [];
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
      match: "/api/adapters/2/ai/assist",
      respond: (body) => {
        requestBodies.push(body ?? "");
        return { body: aiResponse("B 的回复", AI_CANDIDATE) };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();

  selectInEditor("adapter-a selection\n", 1, 1);
  fireEvent.click(screen.getByTestId("add-ai-selection"));
  expect(screen.getByTestId("ai-snippet-label").textContent).toContain("第 1 行");

  // 切换 Adapter：旧片段标记立即消失，按钮回到禁用（不串线）。
  fireEvent.click(screen.getAllByTestId("adapter-item")[1]);
  await screen.findByRole("heading", { name: "adapter-b" });
  expect(screen.queryByTestId("ai-context-snippets")).toBeNull();
  expect((screen.getByTestId("add-ai-selection") as HTMLButtonElement).disabled).toBe(true);

  // B 中新片段 → 标记属于 B；发送只携带 B 的快照。
  selectInEditor("adapter-b selection\n", 7, 8);
  fireEvent.click(screen.getByTestId("add-ai-selection"));
  expect(screen.getByTestId("ai-snippet-label").textContent).toContain("第 7–8 行");
  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "B 的请求" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-candidate-summary");
  const payload = JSON.parse(requestBodies[0]) as {
    context_snippets: { source: string; text: string; start_line: number; end_line: number }[];
  };
  expect(payload.context_snippets).toEqual([
    { source: "code", text: "adapter-b selection\n", start_line: 7, end_line: 8 },
  ]);
});

it("shows DLR lifecycle stage progress and converges on success without reasoning", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  let resolveAssist: ((response: AiAssistResponse) => void) | undefined;
  const pendingAssist = new Promise<AiAssistResponse>((resolve) => {
    resolveAssist = resolve;
  });
  let resolveBindings: ((body: unknown[]) => void) | undefined;
  const pendingBindings = new Promise<unknown[]>((resolve) => {
    resolveBindings = resolve;
  });
  let bindingReads = 0;
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    {
      method: "GET",
      match: "/api/adapters/1/credential-bindings",
      respond: async () => {
        bindingReads += 1;
        if (bindingReads === 1) {
          return { body: [] };
        }
        // 第二次读取（响应校验阶段）挂起，用于观察“正在校验返回结果”。
        return { body: await pendingBindings };
      },
    },
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
    target: { value: "生成候选" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));

  // 阶段 1：点击后先展示“正在准备当前代码上下文”。
  expect(screen.getByTestId("ai-progress-stage").textContent).toBe("正在准备当前代码上下文…");
  // 阶段 2：请求挂起期间展示“正在请求 AI 模型”。
  await screen.findByText("正在请求 AI 模型…");
  expect(screen.queryByTestId("ai-progress-done")).toBeNull();

  // 阶段 3：Provider 返回后、结果校验期间展示“正在校验返回结果”。
  await act(async () => {
    resolveAssist?.(aiResponse("候选已生成", AI_CANDIDATE));
    await pendingAssist;
  });
  await screen.findByText("正在校验返回结果…");
  expect(screen.queryByTestId("ai-candidate-summary")).toBeNull();

  // 阶段 4：校验完成 → 收敛到成功态“已生成修改，等待查看 Diff”+ Candidate。
  await act(async () => {
    resolveBindings?.([]);
    await pendingBindings;
  });
  await screen.findByTestId("ai-candidate-summary");
  expect(screen.getByTestId("ai-progress-done").textContent).toBe("已生成修改，等待查看 Diff");
  expect(screen.queryByTestId("ai-progress-stage")).toBeNull();
  // Provider hidden reasoning 永远不进入对话渲染。
  expect(document.body.textContent).not.toContain("reasoning");
});

it("converges to an explicit error state when the request fails", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    aiBindingsRoute(1),
    aiAttachmentCapabilitiesRoute(),
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: () => ({
        status: 502,
        body: {
          detail: {
            code: "ai_provider_unreachable",
            message: "无法连接模型服务：请检查网络连通性后重试",
          },
        },
      }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "触发失败" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));

  // 失败必须收敛到明确错误状态：进度行与成功态都不复存在。
  await screen.findByTestId("ai-panel-error");
  expect(screen.getByTestId("ai-panel-error").textContent).toContain("无法连接模型服务");
  expect(screen.queryByTestId("ai-progress-stage")).toBeNull();
  expect(screen.queryByTestId("ai-progress-done")).toBeNull();
  expect(screen.queryByTestId("ai-candidate")).toBeNull();
});

it("renders the B2 attachment capability bounds and rejects an oversized file in the composer", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    aiBindingsRoute(1),
    aiAttachmentCapabilitiesRoute({
      limits: {
        max_attachments: 8,
        max_file_bytes: 1024,
        max_total_bytes: 2048,
        max_parsed_chars_per_file: 64 * 1024,
        max_parsed_total_chars: 256 * 1024,
        parse_timeout_seconds: 30,
      },
    }),
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();

  // Server-provided bounds render in the composer hint (1 KiB per-file).
  await screen.findByTestId("ai-attachment-hint");
  expect(screen.getByTestId("ai-attachment-hint").textContent).toContain("1 KiB");
  expect(screen.getByTestId("ai-attachment-add").textContent).toBe("添加附件");

  // A file above the server-declared bound is rejected before any request.
  const oversized = new File([new Uint8Array(2048)], "notes.txt", { type: "text/plain" });
  fireEvent.change(screen.getByTestId("ai-attachment-input"), {
    target: { files: [oversized] },
  });
  await screen.findByTestId("ai-attachment-error");
  expect(screen.getByTestId("ai-attachment-error").textContent).toContain("1 KiB");
  expect(screen.queryByTestId("ai-attachment-item")).toBeNull();
});

it("does not let the old session progress overwrite the new Adapter session", async () => {
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
    aiAttachmentCapabilitiesRoute(),
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
    target: { value: "A 的请求" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-progress-stage");

  // 请求挂起时切换 Adapter：旧进度必须消失，不得覆盖新会话。
  fireEvent.click(screen.getAllByTestId("adapter-item")[1]);
  await screen.findByRole("heading", { name: "adapter-b" });
  expect(screen.queryByTestId("ai-progress-stage")).toBeNull();
  expect(screen.queryByTestId("ai-loading")).toBeNull();

  // 旧响应晚到：不得在 B 会话渲染 Candidate 或成功进度。
  await act(async () => {
    resolveAssistA?.(aiResponse("A 的旧响应", AI_CANDIDATE));
    await pendingAssistA;
  });
  expect(screen.queryByText("A 的旧响应")).toBeNull();
  expect(screen.queryByTestId("ai-candidate")).toBeNull();
  expect(screen.queryByTestId("ai-progress-done")).toBeNull();
});

it("does not claim a ready Diff for a plain-text reply without a Candidate", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    aiBindingsRoute(1),
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: () => ({ body: aiResponse("这是纯文本说明，不生成候选", null) }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "解释一下" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByText("这是纯文本说明，不生成候选");

  // candidate=null：没有可查看的 Diff，成功收敛行不得出现。
  expect(screen.queryByTestId("ai-progress-done")).toBeNull();
  expect(screen.queryByTestId("ai-candidate")).toBeNull();
  // 进度行也必须收敛消失（不残留任何阶段）。
  expect(screen.queryByTestId("ai-progress-stage")).toBeNull();
});

it("keeps Candidate stale and Apply gating unchanged when a selection is present", async () => {
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

  selectInEditor("selected fragment\n", 1, 1);
  fireEvent.click(screen.getByTestId("add-ai-selection"));
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
  // 选区上下文不放松 stale 判定：工作副本变化仍要求“仍然应用”。
  await screen.findByTestId("ai-candidate-stale");
  fireEvent.click(screen.getByTestId("ai-view-diff"));
  await screen.findByTestId("version-diff");
  expect(screen.getByTestId("diff-candidate-stale")).toBeTruthy();
  expect(screen.getByTestId("diff-apply-candidate").textContent).toContain("仍然应用");
  fireEvent.click(screen.getByTestId("diff-apply-candidate"));
  expect(valueOf("code-editor")).toBe("candidate-code\n");
});

// --- M5.5.13：Apply 自动关 Diff / 悬浮入口拖动 / 日志上下文 / 文案收敛 --------

// jsdom 的 PointerEvent 构造器不应用 init 属性：用普通事件 + 显式属性派发，
// 覆盖 React 的合成 pointer 事件处理（真实浏览器行为一致）。
function pointerEvent(type: string, init: Record<string, unknown>) {
  const event = new window.Event(type, { bubbles: true });
  Object.assign(event, init);
  return event;
}

function firePointer(el: Element, type: string, init: Record<string, unknown>) {
  act(() => {
    el.dispatchEvent(pointerEvent(type, init));
  });
}

it("auto-closes the Candidate Diff on successful Apply and keeps it open when blocked", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion({ code: "base-code\n" })),
    aiBindingsRoute(1),
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: () => ({ body: aiResponse("候选已生成", AI_CANDIDATE) }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "改代码" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-candidate-summary");

  // Apply 成功 → Diff 自动关闭（不再需要手工点“关闭”），返回 Workbench。
  fireEvent.click(screen.getByTestId("ai-view-diff"));
  await screen.findByTestId("version-diff");
  fireEvent.click(screen.getByTestId("diff-apply-candidate"));
  await waitFor(() => expect(screen.queryByTestId("version-diff")).toBeNull());
  expect(valueOf("code-editor")).toBe("candidate-code\n");
  expect(screen.getByTestId("ai-candidate-applied")).toBeTruthy();
});

it("keeps the Candidate Diff open with its reason when Apply is blocked by the run lock", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, runtime_locked: true });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion({ code: "base-code\n" })),
    aiBindingsRoute(1),
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: () => ({ body: aiResponse("候选已生成", AI_CANDIDATE) }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();

  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "改代码" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-candidate-summary");

  fireEvent.click(screen.getByTestId("ai-view-diff"));
  await screen.findByTestId("version-diff");
  // 运行锁：Apply 禁用并展示原因，Diff 保持打开（不自动关闭）。
  expect((screen.getByTestId("diff-apply-candidate") as HTMLButtonElement).disabled).toBe(true);
  expect(
    (screen.getByTestId("diff-apply-candidate") as HTMLButtonElement)
      .closest(".action-with-reason")
      ?.getAttribute("aria-label"),
  ).toContain("适配器正在运行，不能应用候选修改");
  expect(screen.getByTestId("version-diff")).toBeTruthy();
  fireEvent.click(screen.getByTestId("diff-close"));
  await waitFor(() => expect(screen.queryByTestId("version-diff")).toBeNull());
});

it("drags the floating entry within the viewport without clicking, and click still opens", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    aiBindingsRoute(1),
  ]);
  render(<App />);
  await selectFirstAdapter();
  const entry = (await screen.findByTestId("open-ai-assistant")) as HTMLButtonElement;
  // 内联定位作用于绝对定位的悬浮宿主（aside），按钮本身不带定位样式。
  const entryHost = entry.closest(".ai-assistant-collapsed") as HTMLElement;
  expect(entryHost).not.toBeNull();
  expect(entryHost.style.left).toBe("");

  // jsdom 无布局：注入按钮的“实际渲染位置”，固化修复后的语义——首次拖动必须
  // 从 getBoundingClientRect() 的当前位置跟随指针，而不是从 (0,0) 瞬移到左上角。
  let renderedLeft = 180;
  let renderedTop = 280;
  entry.getBoundingClientRect = () =>
    ({
      left: renderedLeft,
      top: renderedTop,
      width: 46,
      height: 46,
      right: renderedLeft + 46,
      bottom: renderedTop + 46,
      x: renderedLeft,
      y: renderedTop,
      toJSON: () => ({}),
    }) as DOMRect;

  // 拖动超过阈值：位置从实际渲染位置跟随指针，合成 click 被吞掉（不展开面板）。
  firePointer(entry, "pointerdown", { pointerId: 1, clientX: 200, clientY: 300, button: 0, pointerType: "mouse" });
  firePointer(entry, "pointermove", { pointerId: 1, clientX: 260, clientY: 330 });
  firePointer(entry, "pointerup", { pointerId: 1 });
  fireEvent.click(entry);
  expect(screen.queryByTestId("ai-assistant-panel")).toBeNull();
  expect(entryHost.style.left).toBe("240px");
  expect(entryHost.style.top).toBe("310px");
  // 拖动定位生效时必须禁用默认 translateY(-50%)，钳制坐标即实际渲染坐标。
  expect(entryHost.style.transform).toBe("none");
  renderedLeft = 240;
  renderedTop = 310;

  // 拖动不会拖出页面：极限坐标被钳制在视口内。
  firePointer(entry, "pointerdown", { pointerId: 2, clientX: 260, clientY: 330, button: 0, pointerType: "mouse" });
  firePointer(entry, "pointermove", { pointerId: 2, clientX: 100000, clientY: 100000 });
  firePointer(entry, "pointerup", { pointerId: 2 });
  const clampedX = Number.parseInt(entryHost.style.left, 10);
  const clampedY = Number.parseInt(entryHost.style.top, 10);
  expect(clampedX).toBeLessThanOrEqual(window.innerWidth - 54);
  expect(clampedY).toBeLessThanOrEqual(window.innerHeight - 54);
  expect(clampedX).toBeGreaterThanOrEqual(8);

  // 位置不持久化：重新挂载后恢复产品默认位置（无内联坐标）。
  cleanup();
  render(<App />);
  await selectFirstAdapter();
  const fresh = (await screen.findByTestId("open-ai-assistant")) as HTMLButtonElement;
  expect((fresh.closest(".ai-assistant-collapsed") as HTMLElement).style.left).toBe("");

  // 普通点击（无拖动）仍展开 AI 面板。
  fireEvent.click(fresh);
  await screen.findByTestId("ai-assistant-panel");
});

it("adds masked live-log selections to the AI context alongside code and sends both in order", async () => {
  const adapter = makeAdapter({ latest_version_id: 10, runtime_worker_id: 1 });
  const pending = makeExecution();
  const succeeded = makeExecution({
    status: "succeeded",
    worker_id: 1,
    target_worker_id: 1,
    stdout:
      "[2026-08-17 10:30:00] 任务开始\n[2026-08-17 10:30:01] 任务结束\n",
    output: { ok: true },
    output_size: 11,
    ended_at: "2026-08-15T00:00:02Z",
    duration_ms: 1000,
  });
  const requestBodies: string[] = [];
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    aiBindingsRoute(1),
    {
      method: "GET",
      match: "/api/workers",
      respond: () => ({
        body: [{ id: 1, name: "task-worker", status: "online", last_heartbeat: "", capabilities: ["python"] }],
      }),
    },
    { method: "POST", match: "/api/adapters/1/executions", respond: () => ({ status: 201, body: pending }) },
    { method: "GET", match: "/api/adapters/1", respond: () => ({ body: adapter }) },
    {
      method: "GET",
      match: "/api/executions/5/events",
      respond: () => ({
        stream: `event: log\ndata: ${JSON.stringify({ stream: "stdout", chunk: "[2026-08-17 10:30:00] 任务开始\\n" })}\n\nevent: execution\ndata: ${JSON.stringify(succeeded)}\n\n`,
      }),
    },
    { method: "GET", match: "/api/executions/5", respond: () => ({ body: succeeded }) },
    {
      method: "POST",
      match: "/api/adapters/1/ai/assist",
      respond: (body) => {
        requestBodies.push(body ?? "");
        return { body: aiResponse("候选已生成", AI_CANDIDATE) };
      },
    },
  ]);
  render(<App />);
  await selectFirstAdapter();

  // 先加入一个代码片段（编辑 Tab），面板自动展开。
  selectInEditor("def handle(context, input):\n    return input\n", 1, 1);
  fireEvent.click(screen.getByTestId("add-ai-selection"));
  await screen.findByTestId("ai-assistant-panel");
  expect(screen.getAllByTestId("ai-snippet-label")[0].textContent).toContain("代码");

  // 运行一次 → 自动切到「实时日志」Tab，出现统一日志。
  const runButton = (await screen.findByTestId("header-task-run-once")) as HTMLButtonElement;
  await waitFor(() => expect(runButton.disabled).toBe(false));
  fireEvent.click(runButton);
  await screen.findByTestId("live-log-workspace");
  await waitFor(() => {
    expect(screen.getByTestId("live-log").textContent).toContain("任务开始");
  });

  // 选中日志可见文本（模拟浏览器选区；只读渲染出的已脱敏文本）→ 加入上下文。
  const logPane = screen.getByTestId("live-log");
  const textNode = logPane.firstChild as Text;
  const range = document.createRange();
  range.setStart(textNode, 0);
  range.setEnd(textNode, 31); // 第一行完整（含换行）
  const getSelectionSpy = vi.spyOn(window, "getSelection").mockReturnValue({
    isCollapsed: false,
    rangeCount: 1,
    getRangeAt: () => range,
    toString: () => range.toString(),
    anchorNode: textNode,
    focusNode: textNode,
  } as unknown as Selection);
  act(() => {
    document.dispatchEvent(new Event("selectionchange"));
  });
  const addLogButton = screen.getByTestId("live-log-add-context") as HTMLButtonElement;
  expect(addLogButton.disabled).toBe(false);
  fireEvent.click(addLogButton);
  expect(screen.getAllByTestId("ai-snippet-label")).toHaveLength(2);
  expect(screen.getAllByTestId("ai-snippet-label")[1].textContent).toContain("实时日志");
  expect(screen.getAllByTestId("ai-snippet-label")[1].textContent).toContain("10:30:00");

  // 发送：按加入顺序携带代码 + 日志两个片段（日志只有浏览器可见脱敏文本）。
  fireEvent.change(screen.getByTestId("ai-message-input"), {
    target: { value: "解释代码和日志" },
  });
  fireEvent.click(screen.getByTestId("ai-send"));
  await screen.findByTestId("ai-candidate-summary");
  const payload = JSON.parse(requestBodies[0]) as {
    context_snippets: { source: string; text: string; start_line: number; end_line: number }[];
  };
  expect(payload.context_snippets).toHaveLength(2);
  expect(payload.context_snippets[0]).toEqual({
    source: "code",
    text: "def handle(context, input):\n    return input\n",
    start_line: 1,
    end_line: 1,
  });
  expect(payload.context_snippets[1].source).toBe("log");
  expect(payload.context_snippets[1].text).toContain("[2026-08-17 10:30:00] 任务开始");
  expect(payload.context_snippets[1].start_line).toBe(1);
  getSelectionSpy.mockRestore();
});

it("shows the M5.5.13 credential guidance copy and the quiet assistant header", async () => {
  const adapter = makeAdapter({ latest_version_id: 10 });
  stubFetch([
    ...consoleWithVersionRoutes(adapter, makeVersion()),
    aiBindingsRoute(1),
  ]);
  render(<App />);
  await selectFirstAdapter();
  await openAiAssistant();

  // 引导文案：凭据绑定引导 + 硬编码敏感信息会随代码发送的区分，无内部术语。
  const empty = screen.getByTestId("ai-conversation-empty");
  expect(empty.textContent).toContain("凭据绑定");
  expect(empty.textContent).toContain("避免敏感凭据随代码发送给 AI");
  expect(empty.textContent).not.toContain("工作副本");
  expect(empty.textContent).not.toContain("唯一代码快照");
  // UX-FINAL-002：底部重复提示已移除；上方空状态仍保留安全引导。
  expect(screen.queryByTestId("ai-credential-guidance")).toBeNull();
  // 面板上下文行不使用内部术语“工作副本”。
  expect(screen.getByTestId("ai-current-context").textContent).not.toContain("工作副本");
  // V3 头部保持工作区中性底色，仅用细蓝线标识 AI 面板边界。
  const appStyles = readFileSync(join(process.cwd(), "src/index.css"), "utf8");
  expect(appStyles).toMatch(
    /\.ai-assistant-header\s*\{[^}]*background\s*:\s*var\(--dlr-workspace-bg\)[^}]*border-top\s*:\s*2px solid #1677ff/s,
  );
});
