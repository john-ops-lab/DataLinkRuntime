/**
 * M5.6 Wave 2 B: full Web Console user-facing text internationalization.
 *
 * Focus: immediate zh-CN <-> en switching across the whole Console (Task /
 * Schedule / Webhook workbenches, Settings surfaces, AI Candidate/Diff/Apply),
 * locale-appropriate status/reason/error/fallback text, accessibility names,
 * backend authority, user content immutability, zero Adapter/Revision
 * mutation on locale switch, key parity and English long-copy rendering at
 * the tracked viewport widths.
 *
 * Layout note (recorded limitation): jsdom has no real layout engine, so the
 * 1280/1440/1680/1920 assertions below are functional rendering smokes
 * (English console renders without missing keys or crashes at each width),
 * not pixel measurements. Real-browser layout evidence was not available in
 * this environment and is tracked as a manual verification item in the PR.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import App, { TOKEN_STORAGE_KEY } from "./App";
import { api, setAuthToken } from "./api";
import AiAssistantPanel from "./components/AiAssistantPanel";
import SystemSettingsDrawer from "./components/SystemSettingsDrawer";
import TaskWorkbenchHeader from "./components/TaskWorkbenchHeader";
import {
  applySystemLocale,
  currentSystemLocale,
  DEFAULT_SYSTEM_LOCALE,
  i18n,
  resources,
} from "./i18n";
import { statusLabel } from "./status";
import type { Adapter, AiAssistResponse, VersionDetail } from "./types";
import { userErrorMessage } from "./user-message";

const { monacoHarness } = vi.hoisted(() => {
  const state: { selection: unknown; text: string } = { selection: null, text: "" };
  return {
    monacoHarness: {
      setSelection: (selection: unknown) => {
        state.selection = selection;
      },
      getSelection: () => state.selection,
      setText: (text: string) => {
        state.text = text;
      },
      getText: () => state.text,
      reset: () => {
        state.selection = null;
        state.text = "";
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
        data-original={props.original ?? ""}
        data-modified={props.modified ?? ""}
        data-monaco-language={props.language ?? ""}
      />
    );
  },
  loader: {
    init: () =>
      Promise.resolve({
        editor: { setTheme: () => undefined },
      }),
  },
}));

interface RouteResponse {
  status?: number;
  body?: unknown;
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
    const route = routes.find(
      (candidate) =>
        candidate.method === method &&
        (typeof candidate.match === "string"
          ? candidate.match === url
          : candidate.match.test(url)),
    );
    if (!route) {
      console.error(`UNEXPECTED REQUEST: ${method} ${url}`);
      throw new Error(`Unexpected request: ${method} ${url}`);
    }
    const { status = 200, body } = await route.respond(requestBody, url);
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

function healthRoute(payload: unknown): Route {
  return { method: "GET", match: "/api/health", respond: () => ({ body: payload }) };
}

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
    latest_version_id: 10,
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
    code: "def handle(context, input):\n    return input\n",
    requirements: "",
    runtime_config: {},
    created_at: "2026-08-11T00:00:00Z",
    ...overrides,
  };
}

/** Authenticated console with one Task Adapter that has a saved Revision. */
function taskConsoleRoutes(adapter: Adapter, version: VersionDetail) {
  return [
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/locale", respond: () => ({ body: { locale: currentSystemLocale() } }) },
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [{ id: 10, seq: 1 }] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: version }) },
    { method: "GET", match: /^\/api\/adapters\/1\/schedule$/, respond: () => ({ status: 404, body: { code: "schedule_not_configured" } }) },
    { method: "GET", match: /^\/api\/adapters\/1\/bindings$/, respond: () => ({ body: [] }) },
  ];
}

beforeEach(() => {
  sessionStorage.setItem(TOKEN_STORAGE_KEY, "test-admin-token");
});

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
  sessionStorage.clear();
  window.localStorage.clear();
  setAuthToken(null);
  monacoHarness.reset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("switches the whole Task Console between zh-CN and en immediately without reload", async () => {
  const adapter = makeAdapter({ name: "task-a", run_mode: "schedule" });
  const version = makeVersion();
  stubFetch(taskConsoleRoutes(adapter, version));

  render(<App />);
  const items = await screen.findAllByTestId("adapter-item");
  fireEvent.click(items[0]);
  await screen.findByTestId("code-editor");

  // zh-CN baseline.
  expect(screen.getByRole("tab", { name: "编辑" })).toBeTruthy();
  expect(screen.getByRole("tab", { name: "运行设置" })).toBeTruthy();
  expect(screen.getByRole("tab", { name: "执行记录" })).toBeTruthy();
  expect(screen.getByRole("tab", { name: "实时日志" })).toBeTruthy();
  expect(screen.getByTestId("header-task-schedule-toggle").textContent).toBe("启用定时");
  expect(screen.getByTestId("control-status").textContent).toBe("控制服务正常");
  expect(screen.getByTestId("adapter-catalog").textContent).toContain("定时运行");

  // Switch to English: the same mounted console must update in place.
  await applySystemLocale("en");
  await waitFor(() => expect(screen.getByRole("tab", { name: "Edit" })).toBeTruthy());
  expect(screen.getByRole("tab", { name: "Runtime settings" })).toBeTruthy();
  expect(screen.getByRole("tab", { name: "Executions" })).toBeTruthy();
  expect(screen.getByRole("tab", { name: "Live logs" })).toBeTruthy();
  await waitFor(() => expect(screen.getByTestId("header-task-schedule-toggle").textContent).toBe("Enable schedule"));
  await waitFor(() => expect(screen.getByTestId("control-status").textContent).toBe("Control service healthy"));
  await waitFor(() => expect(screen.getByTestId("adapter-catalog").textContent).toContain("Scheduled"));

  // And back.
  await applySystemLocale("zh-CN");
  await waitFor(() => expect(screen.getByRole("tab", { name: "编辑" })).toBeTruthy());
  await waitFor(() => expect(screen.getByTestId("header-task-schedule-toggle").textContent).toBe("启用定时"));
});

it("switches the Webhook workbench copy and blocked reasons with the locale", async () => {
  const adapter = makeAdapter({
    id: 1,
    name: "hook-a",
    adapter_type: "webhook",
    latest_version_id: null,
  });
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/locale", respond: () => ({ body: { locale: currentSystemLocale() } }) },
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [] }) },
    {
      method: "GET",
      match: "/api/adapters/1/webhook",
      respond: () => ({
        status: 404,
        body: { code: "webhook_not_configured" },
      }),
    },
  ]);

  render(<App />);
  const items = await screen.findAllByTestId("adapter-item");
  fireEvent.click(items[0]);
  await screen.findByTestId("webhook-loading");

  // Webhook panel loads the saved config; seed an unconfigured (404) state so
  // the panel stays in its loading branch. Use the header copy instead, which
  // is locale-reactive immediately.
  expect(screen.getByTestId("webhook-workbench-header").textContent).toContain("Webhook");
  await applySystemLocale("en");
  await waitFor(() =>
    expect(screen.getByTestId("webhook-workbench-header").textContent).toContain("Runtime Worker:"),
  );
  await waitFor(() => expect(screen.getByTestId("header-webhook-toggle").textContent).toBe("Start receiving"));

  // No mutation of any kind happened during the switch.
  const mutations = fetchMock.mock.calls.filter(
    ([url, init]) => String(url).includes("/api/") && (init?.method ?? "GET") !== "GET",
  );
  expect(mutations).toHaveLength(0);
});

it("keeps user-created content untouched and never mutates Adapter/Revision on locale switch", async () => {
  const adapter = makeAdapter({
    id: 1,
    name: "我的自定义适配器",
    description: "中文描述内容，不需要翻译",
    run_mode: "manual",
  });
  const version = makeVersion({
    code: "def handle(context, input):\n    return {\"msg\": \"用户代码\"}\n",
    requirements: "requests==2.32.3",
  });
  const fetchMock = stubFetch(taskConsoleRoutes(adapter, version));

  render(<App />);
  const items = await screen.findAllByTestId("adapter-item");
  fireEvent.click(items[0]);
  await screen.findByTestId("code-editor");

  await applySystemLocale("en");
  expect(screen.getAllByText("我的自定义适配器").length).toBeGreaterThan(0);
  // The user description is not visible text (it lives in the row tooltip),
  // and it must never be translated or altered by the locale switch.
  const itemTitle = screen.getAllByTestId("adapter-item")[0].getAttribute("title") ?? "";
  expect(itemTitle).toContain("中文描述内容，不需要翻译");
  expect((screen.getByTestId("code-editor") as HTMLTextAreaElement).value).toContain(
    "用户代码",
  );
  expect((screen.getByTestId("requirements-input") as HTMLTextAreaElement).value).toBe(
    "requests==2.32.3",
  );

  // No POST/PATCH/PUT/DELETE reached any Adapter/Version/Webhook endpoint.
  const mutations = fetchMock.mock.calls.filter(
    ([url, init]) =>
      String(url).includes("/api/adapters") && (init?.method ?? "GET") !== "GET",
  );
  expect(mutations).toHaveLength(0);
});

it("renders status, reason, error and missing-key fallback text per locale", async () => {
  expect(statusLabel("failed", "zh-CN")).toBe("失败");
  expect(statusLabel("failed", "en")).toBe("Failed");
  expect(statusLabel("unknown-status", "en")).toBe("unknown-status");

  const zhError = userErrorMessage(
    new (await import("./api")).ApiError(500, "boom", "server text"),
    undefined,
    "zh-CN",
  );
  expect(zhError).toContain("请求失败");
  expect(zhError).toContain("错误码：boom");

  const enError = userErrorMessage(
    new (await import("./api")).ApiError(500, "boom", "server text"),
    undefined,
    "en",
  );
  expect(enError).toContain("Request failed");
  expect(enError).toContain("(Error code: boom)");

  await applySystemLocale("zh-CN");
  expect(i18n.t("common.__missing_wave_2_b__")).toBe("暂不可用");
  await applySystemLocale("en");
  expect(i18n.t("common.__missing_wave_2_b__")).toBe("Translation unavailable");
  expect(i18n.t("common.__missing_wave_2_b__")).not.toContain("__missing_wave_2_b__");
});

it("updates disabled-action accessible names and reasons with the locale", async () => {
  const adapter = makeAdapter({ id: 1, latest_version_id: null, runtime_locked: true });
  const runtimeState = {
    scheduleEnabled: false,
    loading: false,
    activeExecution: false,
    canRun: false,
    scheduleEnableBlockedReason: null,
  };
  const { rerender } = render(
    <TaskWorkbenchHeader
      adapter={adapter}
      runtimeWorker={null}
      runtimeState={runtimeState}
      dirty={false}
      busy={false}
      contentReady
      onSave={vi.fn()}
      onOpenSettings={vi.fn()}
      onRunOnce={vi.fn()}
      onStopExecution={vi.fn()}
      onToggleSchedule={vi.fn()}
    />,
  );
  expect(screen.getByLabelText("保存不可用：适配器正在运行，请先停止当前运行后再保存")).toBeTruthy();

  await applySystemLocale("en");
  rerender(
    <TaskWorkbenchHeader
      adapter={adapter}
      runtimeWorker={null}
      runtimeState={runtimeState}
      dirty={false}
      busy={false}
      contentReady
      onSave={vi.fn()}
      onOpenSettings={vi.fn()}
      onRunOnce={vi.fn()}
      onStopExecution={vi.fn()}
      onToggleSchedule={vi.fn()}
    />,
  );
  expect(
    screen.getByLabelText("Save unavailable: Stop the current run before saving"),
  ).toBeTruthy();
});

it("switches AI Candidate/Diff/Apply copy between the two locales", async () => {
  const adapter = makeAdapter({ id: 1, name: "ai-adapter" });
  const candidate: AiAssistResponse = {
    message: "generated",
    provider: "openai",
    model: "test-model",
    candidate: {
      summary: "add pagination",
      code: "def handle(context, input):\n    return input\n",
      requirements: "",
      runtime_config: {},
      required_secret_keys: [],
    },
  };
  vi.spyOn(api, "assistAdapter").mockResolvedValue(candidate);
  vi.spyOn(api, "listAdapterBindings").mockResolvedValue([]);

  const workingCopy = { code: "def handle(context, input):\n    return input\n", requirements: "", runtimeConfigText: "{}" };
  const { rerender } = render(
    <AiAssistantPanel
      open
      adapter={adapter}
      selectedVersionId={10}
      selectedVersionSeq={1}
      workingCopy={workingCopy}
      contentReady
      busy={false}
      contextSnippets={[]}
      theme="vs-dark"
      onOpen={vi.fn()}
      onClose={vi.fn()}
      onApply={vi.fn()}
      onRemoveContextSnippet={vi.fn()}
      onClearContextSnippets={vi.fn()}
    />,
  );
  expect(screen.getByText("AI 助手")).toBeTruthy();
  expect(screen.getByLabelText("AI 指令")).toBeTruthy();

  fireEvent.change(screen.getByTestId("ai-message-input"), { target: { value: "生成候选" } });
  fireEvent.click(screen.getByTestId("ai-send"));
  expect(await screen.findByTestId("ai-candidate-ready")).toBeTruthy();
  expect(screen.getByTestId("ai-candidate-ready").textContent).toBe("代码已生成");
  fireEvent.click(screen.getByTestId("ai-view-diff"));
  expect(await screen.findByTestId("version-diff")).toBeTruthy();
  expect(screen.getByTestId("version-diff").textContent).toContain("当前编辑内容");
  expect(screen.getByTestId("diff-apply-candidate").textContent).toBe("应用修改");

  await applySystemLocale("en");
  rerender(
    <AiAssistantPanel
      open
      adapter={adapter}
      selectedVersionId={10}
      selectedVersionSeq={1}
      workingCopy={workingCopy}
      contentReady
      busy={false}
      contextSnippets={[]}
      theme="vs-dark"
      onOpen={vi.fn()}
      onClose={vi.fn()}
      onApply={vi.fn()}
      onRemoveContextSnippet={vi.fn()}
      onClearContextSnippets={vi.fn()}
    />,
  );
  expect(screen.getByText("AI assistant")).toBeTruthy();
  expect(screen.getByLabelText("AI instruction")).toBeTruthy();
  expect(screen.getByTestId("ai-candidate-ready").textContent).toBe("Code generated");
  // The Candidate diff stays open across the switch and its pane labels are
  // re-derived at render time, exactly like the Workbench diff.
  expect(screen.getByTestId("version-diff").textContent).toContain("Current code");
  expect(screen.getByTestId("version-diff").textContent).toContain("Code");
  expect(screen.getByTestId("version-diff").textContent).toContain("Runtime parameters");
  expect(screen.getByTestId("diff-apply-candidate").textContent).toBe("Apply changes");
});

it("uses locale punctuation in composed labels (guide line, options, Run ID, separators)", async () => {
  // Composed templates keep punctuation inside the locale resources: the
  // English output must never render full-width CJK punctuation.
  await applySystemLocale("en");
  expect(
    i18n.t("credentials.typeOption", { ns: "settings", type: "Token", fields: "token" }),
  ).toBe("Token (token)");
  expect(
    i18n.t("worker.option", { ns: "common", name: "worker-1", status: "Online" }),
  ).toBe("worker-1 (Online)");
  expect(i18n.t("history.runId", { ns: "runtime", id: 6 })).toBe("Run ID: 6");
  expect(i18n.t("punctuation.listSeparator")).toBe("; ");

  // Rendered credential type guide: ASCII punctuation in English.
  vi.spyOn(api, "listCredentials").mockResolvedValue([]);
  vi.spyOn(api, "listPackageSources").mockResolvedValue([]);
  vi.spyOn(api, "getPackageSourceDefaults").mockResolvedValue({
    pypi: { kind: "pypi", name: "PyPI", index_url: "https://pypi.org/simple/" },
    npm: { kind: "npm", name: "npm", index_url: "https://registry.npmjs.org/" },
    maven: { kind: "maven", name: "Maven", index_url: "https://repo1.maven.org/maven2/" },
  });
  vi.spyOn(api, "getAiSetting").mockResolvedValue(null);
  render(<SystemSettingsDrawer open onClose={vi.fn()} />);
  const guide = await screen.findByTestId("credential-type-guide-password");
  const guideText = guide.textContent ?? "";
  expect(guideText).toContain("Password (Fields: username + password)");
  expect(guideText).not.toMatch(/[（）：；。]/);

  // And the same guide renders CJK punctuation in Chinese.
  await applySystemLocale("zh-CN");
  await waitFor(() =>
    expect(screen.getByTestId("credential-type-guide-password").textContent).toContain(
      "密码（字段：username + password）：常见场景为",
    ),
  );
});

it("keeps zh-CN/en key sets identical and renders the English console at 1280–1920 widths", async () => {
  // Key parity across every bundled namespace.
  function leafKeys(value: unknown, prefix = ""): string[] {
    if (typeof value !== "object" || value === null) {
      return [prefix];
    }
    return Object.entries(value).flatMap(([key, child]) =>
      leafKeys(child, prefix === "" ? key : `${prefix}.${key}`),
    );
  }
  for (const namespace of Object.keys(resources[DEFAULT_SYSTEM_LOCALE])) {
    const zhKeys = leafKeys(resources["zh-CN"][namespace as keyof typeof resources.en]);
    const enKeys = leafKeys(resources.en[namespace as keyof typeof resources.en]);
    expect(enKeys, `en.${namespace} keys`).toEqual(zhKeys);
  }

  // Functional rendering smoke at each tracked width. jsdom has no layout
  // engine, so these are not pixel measurements; they pin that the English
  // console renders without missing keys or crashes at every tracked width.
  await applySystemLocale("en");
  const adapter = makeAdapter({ name: "layout-task" });
  const version = makeVersion();
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/locale", respond: () => ({ body: { locale: "en" } }) },
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [{ id: 10, seq: 1 }] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: version }) },
  ]);
  for (const width of [1280, 1440, 1680, 1920]) {
    Object.defineProperty(window, "innerWidth", { value: width, configurable: true });
    const { unmount } = render(<App />);
    const items = await screen.findAllByTestId("adapter-item");
    fireEvent.click(items[0]);
    await screen.findByTestId("code-editor");
    expect(screen.getByRole("tab", { name: "Edit" })).toBeTruthy();
    expect(document.body.textContent).not.toContain("common.");
    expect(document.body.textContent).not.toContain("adapter.");
    unmount();
  }
  Object.defineProperty(window, "innerWidth", { value: 1024, configurable: true });
});

it("reflects the backend locale across refresh without mutating anything", async () => {
  const adapter = makeAdapter({ name: "refresh-a" });
  const version = makeVersion();
  const localeResponse = { locale: "en" };
  const fetchMock = stubFetch([
    healthRoute({ status: "ok", database: true }),
    { method: "GET", match: "/api/locale", respond: () => ({ body: localeResponse }) },
    { method: "GET", match: "/api/adapters", respond: () => ({ body: [adapter] }) },
    { method: "GET", match: "/api/workers", respond: () => ({ body: [] }) },
    { method: "GET", match: "/api/adapters/1/versions", respond: () => ({ body: [{ id: 10, seq: 1 }] }) },
    { method: "GET", match: "/api/adapters/1/versions/10", respond: () => ({ body: version }) },
  ]);

  // A stale zh-CN browser cache must never override the backend authority.
  window.localStorage.setItem("dlr-system-locale", "zh-CN");
  const first = render(<App />);
  await screen.findByTestId("control-status");
  expect(screen.getByTestId("control-status").textContent).toBe("Control service healthy");
  first.unmount();

  const mutations = fetchMock.mock.calls.filter(
    ([url, init]) => String(url).includes("/api/") && (init?.method ?? "GET") !== "GET",
  );
  expect(mutations).toHaveLength(0);
});
