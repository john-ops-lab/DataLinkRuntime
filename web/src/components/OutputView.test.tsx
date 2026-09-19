import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { classifyExecutionOutput } from "../execution-output";
import { applySystemLocale, DEFAULT_SYSTEM_LOCALE } from "../i18n";
import type { Execution } from "../types";
import { OutputView } from "./OutputView";

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
});

function execution(overrides: Record<string, unknown> = {}): Execution {
  return {
    dispatch_backend: "rabbitmq",
    id: 153,
    adapter_id: 1,
    version_id: 1,
    worker_id: null,
    target_worker_id: 1,
    trigger: "manual",
    scheduled_for: null,
    status: "succeeded",
    attempt_count: 1,
    input: null,
    output: null,
    output_size: 4,
    output_truncated: false,
    output_preview: null,
    stdout: "",
    stdout_truncated: false,
    stderr: "",
    stderr_truncated: false,
    error: null,
    created_at: "2026-09-20T00:00:00Z",
    started_at: "2026-09-20T00:00:01Z",
    ended_at: "2026-09-20T00:00:02Z",
    duration_ms: 1000,
    ...overrides,
  } as Execution;
}

function neverStarted(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    dispatch_backend: "rabbitmq",
    attempt_count: 0,
    started_at: null,
    status: "cancelled",
    output: null,
    output_size: null,
    output_truncated: false,
    output_preview: null,
    ...overrides,
  };
}

describe("classifyExecutionOutput", () => {
  it("gives truncation priority over body and contradictory metadata", () => {
    expect(classifyExecutionOutput({
      ...neverStarted(),
      output: { retained: false },
      output_size: 999,
      output_truncated: true,
      output_preview: "partial",
    })).toBe("truncated");
  });

  it("keeps real content but rejects null claims with a non-boolean truncation field", () => {
    expect(classifyExecutionOutput({ output: false, output_truncated: "false" })).toBe("content");
    expect(classifyExecutionOutput({
      output: null,
      output_size: 4,
      output_truncated: "false",
    })).toBe("unknown");
    expect(classifyExecutionOutput({
      output: null,
      output_size: 0,
      output_truncated: 0,
    })).toBe("unknown");
  });

  it.each([
    { output: false, label: "false" },
    { output: 0, label: "zero" },
    { output: "", label: "empty string" },
    { output: [], label: "empty array" },
    { output: {}, label: "empty object" },
  ])("keeps the non-null body $label when size is absent", ({ output }) => {
    expect(classifyExecutionOutput({ output, output_truncated: false })).toBe("content");
  });

  it("recognizes a complete JSON null and rejects an undefined body of the same size", () => {
    expect(classifyExecutionOutput({
      output: null,
      output_size: 4,
      output_truncated: false,
      output_preview: null,
    })).toBe("json-null");
    expect(classifyExecutionOutput({
      output: undefined,
      output_size: 4,
      output_truncated: false,
      output_preview: null,
    })).toBe("unknown");
  });

  it.each(["queued", "cancelled", "expired"])(
    "recognizes a complete never-started %s record",
    (status) => {
      expect(classifyExecutionOutput(neverStarted({ status }))).toBe("empty");
    },
  );

  it("recognizes explicit size zero with a null, undefined, or missing body", () => {
    expect(classifyExecutionOutput({ output: null, output_size: 0 })).toBe("empty");
    expect(classifyExecutionOutput({ output: undefined, output_size: 0 })).toBe("empty");
    expect(classifyExecutionOutput({ output_size: 0 })).toBe("empty");
  });

  it.each([
    { record: { output: null }, label: "missing size" },
    { record: { output: null, output_size: -1 }, label: "negative size" },
    { record: { output: null, output_size: 0.5 }, label: "fractional size" },
    { record: { output: null, output_size: Number.POSITIVE_INFINITY }, label: "infinite size" },
    { record: { output: null, output_size: Number.NaN }, label: "NaN size" },
    { record: { output: null, output_size: 5 }, label: "incorrect positive size" },
    { record: { output: null, output_size: 4, output_preview: "" }, label: "empty preview string" },
    { record: { output: null, output_size: 0, output_preview: "partial" }, label: "preview with zero size" },
  ] satisfies Array<{ record: Record<string, unknown>; label: string }>)(
    "keeps $label as unknown",
    ({ record }) => {
      expect(classifyExecutionOutput(record)).toBe("unknown");
    },
  );

  it.each(["dispatch_backend", "attempt_count", "started_at", "status"])(
    "requires the never-started field %s to be an own property",
    (field) => {
      const record = neverStarted();
      delete record[field];
      expect(classifyExecutionOutput(record)).toBe("unknown");
    },
  );

  it("does not treat status, a previous attempt, or an absent body as never-started proof", () => {
    expect(classifyExecutionOutput({ status: "cancelled", output: null })).toBe("unknown");
    expect(classifyExecutionOutput(neverStarted({ attempt_count: 1 }))).toBe("unknown");
    const missingBody = neverStarted();
    delete missingBody.output;
    expect(classifyExecutionOutput(missingBody)).toBe("unknown");
    expect(classifyExecutionOutput(neverStarted({ output: undefined }))).toBe("unknown");
  });

  it("treats a never-started identity paired with JSON-null size as contradictory", () => {
    expect(classifyExecutionOutput(neverStarted({ output_size: 4 }))).toBe("unknown");
  });
});

describe("OutputView", () => {
  it.each([
    { output: false, rendered: "false" },
    { output: 0, rendered: "0" },
    { output: "", rendered: "\"\"" },
    { output: [], rendered: "[]" },
    { output: {}, rendered: "{}" },
  ])("renders the complete JSON body $rendered without size metadata", ({ output, rendered }) => {
    render(<OutputView execution={execution({ output, output_size: undefined })} />);
    expect(screen.getByTestId("output-content").textContent).toBe(rendered);
  });

  it("keeps the truncated preview branch ahead of null classification", () => {
    render(<OutputView execution={execution({
      output: null,
      output_size: 4,
      output_truncated: true,
      output_preview: "nul",
    })} />);

    expect(screen.getByTestId("output-truncated").textContent).toContain("输出超过平台保存上限");
    expect(screen.getByTestId("output-preview").textContent).toBe("nul");
  });

  it("renders legal JSON null as content and explicit empty output separately", () => {
    const { rerender } = render(<OutputView execution={execution()} />);
    expect(screen.getByTestId("output-content").textContent).toBe("null");

    rerender(<OutputView execution={execution({ output_size: 0 })} />);
    expect(screen.getByTestId("output-empty").textContent).toContain("无 Output");
  });

  it("renders the neutral unknown message in both locales", async () => {
    const { rerender } = render(<OutputView execution={execution({ output_size: null })} />);
    expect(screen.getByTestId("output-unknown").textContent).toContain("输出信息不足，无法确认");

    await applySystemLocale("en");
    rerender(<OutputView execution={execution({ output_size: null })} />);
    expect(screen.getByTestId("output-unknown").textContent).toContain(
      "Output information is incomplete and cannot be confirmed.",
    );
  });

  it("does not mutate the historical execution while classifying and rendering it", () => {
    const historical = Object.freeze(execution({
      status: "cancelled",
      output: null,
      output_size: null,
      attempt_count: 0,
      started_at: null,
    }));
    const before = structuredClone(historical);

    expect(classifyExecutionOutput(historical)).toBe("empty");
    render(<OutputView execution={historical} />);

    expect(historical).toEqual(before);
    expect(Object.isFrozen(historical)).toBe(true);
  });
});
