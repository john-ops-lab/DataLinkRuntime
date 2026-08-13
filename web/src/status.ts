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
// 生产入口状态与单次 Execution 状态分开展示。“停止中”由
// stopped + active Execution 派生；生产入口仍为 running 但当前无子进程是
// 合法的“已启动 / 空闲”，只有最近生产执行 failed/timeout 才派生异常。

export type ProductionDisplayState =
  | "unpublished"
  | "ready"
  | "running"
  | "stopping"
  | "stopped"
  | "abnormal"
  | "archived";

export const PRODUCTION_STATE_LABELS: Record<ProductionDisplayState, string> = {
  unpublished: "未发布",
  ready: "待启动",
  running: "已启动",
  stopping: "停止中",
  stopped: "已停止",
  abnormal: "异常",
  archived: "已归档",
};

// antd Tag/Badge share the same color vocabulary: keep it consistent here.
export const PRODUCTION_STATE_COLORS: Record<ProductionDisplayState, string> = {
  unpublished: "default",
  ready: "processing",
  running: "success",
  stopping: "processing",
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
    const lastStatus = adapter.last_production_execution_status;
    const hasActiveExecution =
      adapter.running_execution_id !== null && adapter.running_execution_id !== undefined;
    return !hasActiveExecution &&
      (lastStatus === "failed" || lastStatus === "timeout")
      ? "abnormal"
      : "running";
  }
  if (state === "stopped") {
    return adapter.running_execution_id !== null && adapter.running_execution_id !== undefined
      ? "stopping"
      : "stopped";
  }
  return "ready";
}

/** M5.1: Locked production version takes priority when running; falls back to
 * active Execution version, then last production version. */
export function productionRunningVersionId(adapter: Adapter): number | null {
  if ((adapter.production_state ?? "idle") !== "running") {
    return adapter.running_version_id ?? null;
  }
  return (
    adapter.production_version_id ??
    adapter.running_version_id ??
    adapter.last_production_version_id ??
    null
  );
}

export function isProductionStopping(adapter: Adapter): boolean {
  return productionDisplayState(adapter) === "stopping";
}

export function productionStateLabel(state: ProductionDisplayState): string {
  return PRODUCTION_STATE_LABELS[state];
}

export function productionStateColor(state: ProductionDisplayState): string {
  return PRODUCTION_STATE_COLORS[state];
}
