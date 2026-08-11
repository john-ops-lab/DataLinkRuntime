import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import App, { TOKEN_STORAGE_KEY } from "./App";
import { setAuthToken } from "./api";
import type {
  Adapter,
  Execution,
  ExecutionSummary,
  VersionDetail,
  VersionSummary,
} from "./types";

// The Monaco editor is replaced by a plain textarea so tests exercise the DLR
// business integration (value / change / save) instead of the editor itself.
// readOnly is honored so the mutation-time interaction lock is testable.
vi.mock("@monaco-editor/react", () => ({
  default: function Editor(props: {
    value?: string;
    onChange?: (value: string | undefined) => void;
    options?: { readOnly?: boolean };
  }) {
    return (
      <textarea
        data-testid="code-editor"
        value={props.value ?? ""}
        disabled={props.options?.readOnly ?? false}
        onChange={(event) => props.onChange?.(event.target.value)}
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

function makeAdapter(overrides: Partial<Adapter> = {}): Adapter {
  return {
    id: 1,
    name: "adapter-a",
    description: "",
    language: "python",
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
  setAuthToken(null);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
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
    expect(screen.getAllByTestId("adapter-item").map((item) => item.textContent)).toEqual([
      "adapter-a",
      "adapter-b",
    ]);
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
        const payload = JSON.parse(body ?? "{}") as { name: string; description: string };
        const created = makeAdapter({
          name: payload.name,
          description: payload.description,
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
  fireEvent.change(screen.getByTestId("new-adapter-name"), { target: { value: "cmdb-sync" } });
  fireEvent.change(screen.getByTestId("new-adapter-description"), {
    target: { value: "sync cmdb" },
  });
  fireEvent.click(screen.getByTestId("create-adapter"));

  // Created adapter becomes selected and shows its metadata.
  await screen.findByRole("heading", { name: "cmdb-sync" });
  expect(valueOf("adapter-name")).toBe("cmdb-sync");

  const createCall = fetchMock.mock.calls.find(
    ([url, init]) => url === "/api/adapters" && init?.method === "POST",
  );
  expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
    name: "cmdb-sync",
    description: "sync cmdb",
  });
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
  expect(valueOf("version-selector")).toBe("10");
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

  fireEvent.change(screen.getByTestId("version-selector"), { target: { value: "10" } });
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

  fireEvent.click(screen.getByTestId("publish-version"));
  await screen.findByTestId("published-badge");
  expect(screen.getByTestId("latest-badge")).toBeTruthy();
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
  expect((screen.getByTestId("save-version") as HTMLButtonElement).disabled).toBe(true);
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
  expect(valueOf("version-selector")).toBe("10");
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
    {
      method: "POST",
      match: "/api/adapters/1/versions/10/publish",
      respond: async () => ({ body: await publishResponse }),
    },
  ]);
  render(<App />);
  await selectFirstAdapter();
  expect(valueOf("code-editor")).toBe("code-a");

  // Publish(A) starts and stays pending.
  fireEvent.click(screen.getByTestId("publish-version"));
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
  expect((screen.getByTestId("version-selector") as HTMLSelectElement).disabled).toBe(true);

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
  fireEvent.click(runButton);

  expect(screen.queryByTestId("error-banner")).toBeNull();
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) => String(url) === "/api/adapters/1/executions" && init?.method === "POST",
    ),
  ).toBe(false);
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
  const pageOne = { items: [makeSummary({ id: 6 })], next_before_id: 6 };
  const pageTwo = { items: [makeSummary({ id: 4, status: "failed" })], next_before_id: null };
  const detail = makeExecution({ id: 6, input: { k: 1 }, output: { ok: true } });
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

  fireEvent.click(screen.getAllByTestId("history-row")[0]);
  const detailInput = await screen.findByTestId("detail-input");
  expect(detailInput.textContent).toContain('"k": 1');
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

  // Click A (slow), then B (fast): B must win even though A resolves last.
  fireEvent.click(rows[0]);
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
    capabilities: [],
  };
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    emptyAdaptersRoute,
    { method: "GET", match: "/api/workers", respond: () => ({ body: [offlineWorker] }) },
  ]);
  const { unmount } = render(<App />);
  fireEvent.click(await screen.findByTestId("worker-status"));
  await screen.findAllByTestId("worker-item");
  expect(
    screen.getByTestId("worker-status").querySelector(".ant-badge-status-error"),
  ).toBeTruthy();
  expect(
    screen.getByTestId("worker-status").querySelector(".ant-badge-status-success"),
  ).toBeNull();
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
  expect(
    screen.getByTestId("worker-status").querySelector(".ant-badge-status-success"),
  ).toBeTruthy();
});
