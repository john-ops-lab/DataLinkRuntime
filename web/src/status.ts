/** Unified Chinese status display for Executions (M3 spec §8.3). */

import type { ExecutionStatus } from "./types";

export const STATUS_LABELS: Record<ExecutionStatus, string> = {
  pending: "等待中",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  timeout: "超时",
  cancelled: "已取消",
};

// antd Tag color semantics: keep success/error/warning consistent everywhere.
export const STATUS_COLORS: Record<ExecutionStatus, string> = {
  pending: "default",
  running: "processing",
  succeeded: "success",
  failed: "error",
  timeout: "warning",
  cancelled: "default",
};

export const TERMINAL_STATUSES: ReadonlySet<ExecutionStatus> = new Set([
  "succeeded",
  "failed",
  "timeout",
  "cancelled",
]);

export function statusLabel(status: string): string {
  return STATUS_LABELS[status as ExecutionStatus] ?? status;
}

export function statusColor(status: string): string {
  return STATUS_COLORS[status as ExecutionStatus] ?? "default";
}

export function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.has(status as ExecutionStatus);
}
