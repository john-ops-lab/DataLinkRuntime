import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import App from "./App";
import type { Adapter, VersionDetail, VersionSummary } from "./types";

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
}

interface Route {
  method: string;
  match: string | RegExp;
  respond: (body: string | null) => RouteResponse | Promise<RouteResponse>;
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
    const { status = 200, body } = await route.respond(requestBody);
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
  expect(screen.getByTestId("error-banner").textContent).toContain("Version saved");

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
  expect(screen.getByTestId("error-banner").textContent).toContain("Version saved");
  expect(screen.getByTestId("error-banner").textContent).toContain("refreshing the adapter");
  expect(screen.queryByTestId("dirty-indicator")).toBeNull();
  expect(valueOf("code-editor")).toBe("saved code");
  expect(
    fetchMock.mock.calls.some(([url]) => String(url) === "/api/adapters/1"),
  ).toBe(true);
});
