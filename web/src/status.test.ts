import { expect, it } from "vitest";

import { isTerminal, statusColor, statusLabel } from "./status";

it("labels every current Execution state in Chinese", () => {
  expect(statusLabel("pending")).toBe("等待中");
  expect(statusLabel("running")).toBe("运行中");
  expect(statusLabel("queued")).toBe("已排队");
  expect(statusLabel("retry_wait")).toBe("等待重试");
  expect(statusLabel("succeeded")).toBe("成功");
  expect(statusLabel("failed")).toBe("失败");
  expect(statusLabel("timeout")).toBe("超时");
  expect(statusLabel("cancelled")).toBe("已取消");
  expect(statusLabel("dead_letter")).toBe("死信");
  expect(statusLabel("expired")).toBe("已过期");
});

it("treats only persisted terminal states as terminal", () => {
  expect(isTerminal("pending")).toBe(false);
  expect(isTerminal("running")).toBe(false);
  for (const status of ["succeeded", "failed", "timeout", "cancelled", "dead_letter", "expired"]) {
    expect(isTerminal(status)).toBe(true);
  }
  expect(isTerminal("queued")).toBe(false);
  expect(isTerminal("retry_wait")).toBe(false);
});

it("keeps status colors stable for Workbench and history", () => {
  expect(statusColor("pending")).toBe("default");
  expect(statusColor("running")).toBe("processing");
  expect(statusColor("queued")).toBe("processing");
  expect(statusColor("retry_wait")).toBe("warning");
  expect(statusColor("succeeded")).toBe("success");
  expect(statusColor("failed")).toBe("error");
  expect(statusColor("timeout")).toBe("warning");
  expect(statusColor("cancelled")).toBe("default");
  expect(statusColor("dead_letter")).toBe("error");
  expect(statusColor("expired")).toBe("default");
});

it("falls back gracefully for unknown statuses", () => {
  expect(statusLabel("unknown-status")).toBe("unknown-status");
  expect(statusColor("unknown-status")).toBe("default");
  expect(isTerminal("unknown-status")).toBe(false);
  expect(statusLabel("")).toBe("");
  expect(statusColor("")).toBe("default");
  expect(isTerminal("")).toBe(false);
});
