/** Unified Chinese status display for Executions (M3 spec §8.3). */

import type { ExecutionStatus } from "./types";
import { currentSystemLocale, i18n } from "./i18n";

const STATUS_LABEL_KEYS: Record<ExecutionStatus, string> = {
  pending: "executionStatus.pending",
  running: "executionStatus.running",
  queued: "executionStatus.queued",
  retry_wait: "executionStatus.retry_wait",
  succeeded: "executionStatus.succeeded",
  failed: "executionStatus.failed",
  timeout: "executionStatus.timeout",
  cancelled: "executionStatus.cancelled",
  dead_letter: "executionStatus.dead_letter",
  expired: "executionStatus.expired",
};

export const STATUS_LABELS: Record<ExecutionStatus, string> = {
  pending: i18n.getFixedT("zh-CN", "runtime")(STATUS_LABEL_KEYS.pending),
  running: i18n.getFixedT("zh-CN", "runtime")(STATUS_LABEL_KEYS.running),
  queued: i18n.getFixedT("zh-CN", "runtime")(STATUS_LABEL_KEYS.queued),
  retry_wait: i18n.getFixedT("zh-CN", "runtime")(STATUS_LABEL_KEYS.retry_wait),
  succeeded: i18n.getFixedT("zh-CN", "runtime")(STATUS_LABEL_KEYS.succeeded),
  failed: i18n.getFixedT("zh-CN", "runtime")(STATUS_LABEL_KEYS.failed),
  timeout: i18n.getFixedT("zh-CN", "runtime")(STATUS_LABEL_KEYS.timeout),
  cancelled: i18n.getFixedT("zh-CN", "runtime")(STATUS_LABEL_KEYS.cancelled),
  dead_letter: i18n.getFixedT("zh-CN", "runtime")(STATUS_LABEL_KEYS.dead_letter),
  expired: i18n.getFixedT("zh-CN", "runtime")(STATUS_LABEL_KEYS.expired),
};

// antd Tag color semantics: keep success/error/warning consistent everywhere.
export const STATUS_COLORS: Record<ExecutionStatus, string> = {
  pending: "default",
  running: "processing",
  queued: "processing",
  retry_wait: "warning",
  succeeded: "success",
  failed: "error",
  timeout: "warning",
  cancelled: "default",
  dead_letter: "error",
  expired: "default",
};

export const TERMINAL_STATUSES: ReadonlySet<ExecutionStatus> = new Set([
  "succeeded",
  "failed",
  "timeout",
  "cancelled",
  "dead_letter",
  "expired",
]);

export function statusLabel(status: string, locale = currentSystemLocale()): string {
  const key = STATUS_LABEL_KEYS[status as ExecutionStatus];
  return key === undefined ? status : i18n.getFixedT(locale, "runtime")(key);
}

export function statusColor(status: string): string {
  return STATUS_COLORS[status as ExecutionStatus] ?? "default";
}

export function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.has(status as ExecutionStatus);
}
