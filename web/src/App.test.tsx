import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import App from "./App";
import type { Adapter, VersionDetail, VersionSummary } from "./types";

// The Monaco editor is replaced by a plain textarea so tests exercise the DLR
// business integration (value / change / save) instead of the editor itself.
vi.mock("@monaco-editor/react", () => ({
  default: function Editor(props: {
    value?: string;
    onChange?: (value: string | undefined) => void;
  }) {
    return (
      <textarea
        data-testid="code-editor"
        value={props.value ?? ""}
        onChange={(event) => props.onChange?.(event.target.value)}
      />
    );
  },
}));

const STARTER_CODE = "def handle(context, input):\n    return input\n";

interface RouteResponse {
  status?: number;
  body?: unknown;
}

interface Route {
  method: string;
  match: string | RegExp;
  respond: (body: string | null) => RouteResponse;
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
    const { status = 200, body } = route.respond(requestBody);
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

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// --- Control health indicator (kept from M0) --------------------------------

it("shows ok when control health is ok", async () => {
  stubFetch([
    healthRoute({ status: "ok", database: true }),
    emptyAdaptersRoute,
  ]);
  render(<App />);
  await waitFor(() => {
    expect(screen.getByTestId("control-status").textContent).toBe("Control: ok");
  });
});

it("shows degraded when control returns 503 with a valid health payload", async () => {
  stubFetch([
    healthRoute({ status: "degraded", database: false }, 503),
    emptyAdaptersRoute,
  ]);
  render(<App />);
  await waitFor(() => {
    expect(screen.getByTestId("control-status").textContent).toBe("Control: degraded");
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
    expect(screen.getByTestId("control-status").textContent).toBe("Control: unreachable");
  });
});

it("does not show ok for the contradictory payload {status: ok, database: false}", async () => {
  stubFetch([healthRoute({ status: "ok", database: false }), emptyAdaptersRoute]);
  render(<App />);
  await waitFor(() => {
    expect(screen.getByTestId("control-status").textContent).toBe("Control: unreachable");
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
  await screen.findByText("No adapters yet.");

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
