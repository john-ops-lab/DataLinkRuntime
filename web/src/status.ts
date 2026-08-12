/** Unified Chinese status display for Executions (M3 spec §8.3). */

import type { Adapter, ExecutionStatus } from "./types";

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

// --- M3.2: Adapter production display states -------------------------------
// 四层状态（未发布/待启动/已启动/已停止）加上两个纯展示派生：异常（无 active
// Execution 但状态仍是 running，后端不变量被打破时的可见兜底）与已归档。

export type ProductionDisplayState =
  | "unpublished"
  | "ready"
  | "running"
  | "stopped"
  | "abnormal"
  | "archived";

export const PRODUCTION_STATE_LABELS: Record<ProductionDisplayState, string> = {
  unpublished: "未发布",
  ready: "待启动",
  running: "已启动",
  stopped: "已停止",
  abnormal: "异常",
  archived: "已归档",
};

// antd Tag/Badge share the same color vocabulary: keep it consistent here.
export const PRODUCTION_STATE_COLORS: Record<ProductionDisplayState, string> = {
  unpublished: "default",
  ready: "processing",
  running: "success",
  stopped: "default",
  abnormal: "error",
  archived: "warning",
};

export function productionDisplayState(adapter: Adapter): ProductionDisplayState {
  if (adapter.archived_at) {
    return "archived";
  }
  if (adapter.published_version_id === null || adapter.published_version_id === undefined) {
    return "unpublished";
  }
  const state = adapter.production_state ?? "idle";
  if (state === "running") {
    // 后端不变量：running 必有 active Production Execution。字段为 null 时用
    // 异常展示兜底；字段缺失（存量桩数据）则按已启动展示。
    return adapter.running_execution_id === null ? "abnormal" : "running";
  }
  if (state === "stopped") {
    return "stopped";
  }
  return "ready";
}

export function productionStateLabel(state: ProductionDisplayState): string {
  return PRODUCTION_STATE_LABELS[state];
}

export function productionStateColor(state: ProductionDisplayState): string {
  return PRODUCTION_STATE_COLORS[state];
}
