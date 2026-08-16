import { expect, it } from "vitest";

import { isTerminal, statusColor, statusLabel } from "./status";

it("labels every current Execution state in Chinese", () => {
  expect(statusLabel("pending")).toBe("等待中");
  expect(statusLabel("running")).toBe("运行中");
  expect(statusLabel("succeeded")).toBe("成功");
  expect(statusLabel("failed")).toBe("失败");
  expect(statusLabel("timeout")).toBe("超时");
  expect(statusLabel("cancelled")).toBe("已取消");
});

it("treats only persisted terminal states as terminal", () => {
  expect(isTerminal("pending")).toBe(false);
  expect(isTerminal("running")).toBe(false);
  for (const status of ["succeeded", "failed", "timeout", "cancelled"]) {
    expect(isTerminal(status)).toBe(true);
  }
});

it("keeps status colors stable for Workbench and history", () => {
  expect(statusColor("pending")).toBe("default");
  expect(statusColor("running")).toBe("processing");
  expect(statusColor("succeeded")).toBe("success");
  expect(statusColor("failed")).toBe("error");
  expect(statusColor("timeout")).toBe("warning");
  expect(statusColor("cancelled")).toBe("default");
});

it("falls back gracefully for unknown statuses", () => {
  expect(statusLabel("unknown-status")).toBe("unknown-status");
  expect(statusColor("unknown-status")).toBe("default");
});
