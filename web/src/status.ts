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

// --- M3.2/M5.1: Adapter production display states ---------------------------
// 生产入口状态与单次 Execution 状态分开展示。“停止中”由
// stopped + active Execution 派生；production_state=running 且当前无 active
// Execution 是合法的“已启动 / 空闲”（M5.1 起 Start 不再创建 Execution）。
// 上一生命周期的 failed/timeout 不再把生产入口派生成异常，只作为独立的
// “最近一次生产执行失败”结果提示（见 hasLastProductionExecutionFailure）。

export type ProductionDisplayState =
  | "unpublished"
  | "ready"
  | "running"
  | "stopping"
  | "stopped"
  | "archived";

export const PRODUCTION_STATE_LABELS: Record<ProductionDisplayState, string> = {
  unpublished: "未发布",
  ready: "待启动",
  running: "已启动",
  stopping: "停止中",
  stopped: "已停止",
  archived: "已归档",
};

// antd Tag/Badge share the same color vocabulary: keep it consistent here.
export const PRODUCTION_STATE_COLORS: Record<ProductionDisplayState, string> = {
  unpublished: "default",
  ready: "processing",
  running: "success",
  stopping: "processing",
  stopped: "default",
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
    // M5.1: an open entry without an active Execution is the legal idle state,
    // whatever the previous lifecycle's last Execution ended with.
    return "running";
  }
  if (state === "stopped") {
    return hasActiveProductionExecution(adapter) ? "stopping" : "stopped";
  }
  return "ready";
}

/** M5.1: a real active production Execution exists only while one is pending
 * or running; an open production entry without one is the legal idle state. */
export function hasActiveProductionExecution(adapter: Adapter): boolean {
  return adapter.running_execution_id !== null && adapter.running_execution_id !== undefined;
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

/** M5.1: server-derived seq of the version shown for the production entry.
 * Uses the locked production_version_seq first so unvisited Catalog rows never
 * depend on a locally loaded version list. */
export function productionRunningVersionSeq(adapter: Adapter): number | null | undefined {
  const versionId = productionRunningVersionId(adapter);
  if (versionId === null) {
    return undefined;
  }
  if (adapter.production_version_id === versionId) {
    return adapter.production_version_seq;
  }
  if (adapter.running_version_id === versionId) {
    return adapter.running_version_seq;
  }
  return adapter.last_production_version_seq;
}

/** M5.1: the most recent Production Execution ended in failure. This is an
 * Execution-result fact, never a lifecycle state: the open production entry
 * stays “已启动 / 空闲” and never asks for another Stop → Start round trip. */
export function hasLastProductionExecutionFailure(adapter: Adapter): boolean {
  const lastStatus = adapter.last_production_execution_status;
  return lastStatus === "failed" || lastStatus === "timeout";
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
