import { beforeAll, describe, expect, it, vi } from "vitest";

const loaderConfig = vi.hoisted(() => vi.fn());

vi.mock("@monaco-editor/react", () => ({
  loader: { config: loaderConfig },
}));

vi.mock("monaco-editor", () => ({
  editor: {},
}));

function workerMock(kind: string) {
  return {
    default: class {
      readonly kind = kind;
    },
  };
}

vi.mock("monaco-editor/editor/editor.worker?worker", () => workerMock("editor"));
vi.mock("monaco-editor/language/css/css.worker?worker", () => workerMock("css"));
vi.mock("monaco-editor/language/html/html.worker?worker", () => workerMock("html"));
vi.mock("monaco-editor/language/json/json.worker?worker", () => workerMock("json"));
vi.mock("monaco-editor/language/typescript/ts.worker?worker", () => workerMock("typescript"));
vi.mock("monaco-editor/languages/definitions/python/register", () => ({}));

type TaggedWorker = Worker & { kind: string };

function workerKind(label: string): string {
  const worker = self.MonacoEnvironment?.getWorker?.("workerMain.js", label) as TaggedWorker;
  return worker.kind;
}

beforeAll(async () => {
  await import("./editor-setup");
});

describe("Monaco worker routing", () => {
  it.each([
    ["javascript", "typescript"],
    ["typescript", "typescript"],
    ["json", "json"],
    ["css", "css"],
    ["scss", "css"],
    ["less", "css"],
    ["html", "html"],
    ["handlebars", "html"],
    ["razor", "html"],
    ["python", "editor"],
    ["unknown", "editor"],
  ])("routes %s to the %s worker", (label, expected) => {
    expect(workerKind(label)).toBe(expected);
  });

  it("configures the React loader with the bundled Monaco instance", () => {
    expect(loaderConfig).toHaveBeenCalledOnce();
    const options = loaderConfig.mock.calls[0]?.[0] as { monaco?: { editor?: unknown } };
    expect(options.monaco?.editor).toEqual({});
  });
});
