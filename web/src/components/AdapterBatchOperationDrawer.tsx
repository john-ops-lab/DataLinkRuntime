import { Alert, Button, Drawer } from "antd";
import { useTranslation } from "react-i18next";
import { ApiError } from "../api";
import type { AdapterBatchItem } from "../adapter-batch-types";
import type { AdapterBatchController } from "../hooks/useAdapterBatchOperation";
import { useOverlayFocus } from "../hooks/useOverlayFocus";
import { isTerminal, statusLabel } from "../status";
import "./adapter-batch.css";

export default function AdapterBatchOperationDrawer({ controller, onInspect }: {
  controller: AdapterBatchController;
  onInspect: (adapterId: number) => void;
}) {
  const { t, i18n } = useTranslation(["adapter", "common", "runtime"]);
  const { operation, working, querying, unknown, missing } = controller;
  const restoreFocus = useOverlayFocus(controller.open, '[data-testid="batch-select-toggle"], a[href="/adapters"]');
  const blocked = working !== null || querying || unknown || missing;
  const pending = operation?.items.filter(item => item.status === "pending").length ?? 0;
  const failed = operation?.items.filter(item => item.status === "failed").length ?? 0;
  const retryable = operation?.items.filter(item => item.status === "failed" && item.retryable).length ?? 0;
  const completed = operation?.items.filter(item => ["accepted", "succeeded", "no_change"].includes(item.status)).length ?? 0;
  const inProgress = operation?.items.filter(item => item.status === "stopping").length ?? 0;
  const unconfirmed = operation?.items.filter(item => item.status === "unknown").length ?? 0;
  const alreadySubmitted = operation?.items.some(item => item.status !== "pending" && item.status !== "failed") ?? false;

  function reasonText(code: string | null): string {
    if (!code) return "";
    for (const [ns, key] of [["adapter", `batch.errors.${code}`], ["common", `errors.${code}`],
      ["subscription", `errors.${code}`], ["subscription", `queues.errors.${code}`], ["subscription", `kubernetes.errors.${code}`]]) {
      if (!i18n.exists(key, { ns })) continue;
      const text = i18n.t(key, { ns });
      if (!text.includes("{{")) return text;
    }
    return t("batch.failureFallback");
  }

  function resultText(item: AdapterBatchItem): string {
    if (item.status === "succeeded") {
      if (item.action === "cancel_executions") {
        return t(item.executions.length > 0 && item.executions.every(execution => execution.status === "cancelled")
          ? "batch.result.cancelled" : "batch.result.executionsEnded");
      }
      if (item.action === "enable_schedule") return t("batch.result.scheduleEnabled");
      if (item.action === "disable_schedule") return t("batch.result.scheduleDisabled");
      if (item.action === "enable_webhook" || item.action === "enable_subscription") return t("batch.result.receivingEnabled");
      if (item.action === "disable_webhook" || item.action === "disable_subscription") return t("batch.result.receivingStopped");
    }
    if (item.status === "accepted" && item.action === "run_once") return t("batch.result.executionSubmitted");
    if (item.status === "stopping" && item.action === "cancel_executions") return t("batch.result.cancellationRequested");
    return t(`batch.status.${item.status}`);
  }

  return <Drawer open={controller.open} onClose={controller.close} afterOpenChange={restoreFocus}
    title={t("batch.title")} width="min(760px, calc(100vw - 16px))" destroyOnHidden
    extra={<Button onClick={() => void controller.query()} loading={querying} disabled={working !== null || !controller.operationId}>
      {t("batch.refresh")}
    </Button>}
    footer={<div className="adapter-batch-footer">
      <Button onClick={controller.close}>{t("batch.close")}</Button>
      {missing && controller.recoverPreparation && <Button disabled={working !== null || querying}
        onClick={() => void controller.recoverPreparation?.()}>{t("batch.restorePreparation")}</Button>}
      {pending > 0 && <Button type="primary" loading={working === "execute"} disabled={blocked}
        onClick={() => void controller.execute()}>{t(alreadySubmitted ? "batch.continue" : "batch.confirm", { count: pending })}</Button>}
      {retryable > 0 && <Button disabled={blocked} loading={working === "retry"}
        onClick={() => void controller.retry()}>{t("batch.retry", { count: retryable })}</Button>}
    </div>}>
    <div className="adapter-batch-content" data-testid="adapter-batch-result">
      <Alert type="info" showIcon message={t("batch.savedOnly")} description={t("batch.stopBoundary")} />
      {working === "prepare" && <p role="status">{t("batch.preparing")}</p>}
      {unknown && <Alert type="warning" showIcon message={t("batch.unknownTitle")} description={t("batch.unknownHelp")} />}
      {missing && <Alert type="warning" showIcon message={t("batch.missingTitle")}
        description={controller.recoverPreparation ? t("batch.missingRestore") : t("batch.missingNew")} />}
      {!unknown && !missing && controller.error !== null && <Alert type="error" showIcon
        message={reasonText(controller.error instanceof ApiError ? controller.error.code : "unknown_error")} />}
      {operation && <>
        <p className="adapter-batch-summary" role="status" aria-live="polite">{t("batch.summary", {
          total: operation.items.length, completed, pending, failed, stopping: inProgress, unknown: unconfirmed,
        })}</p>
        <ul className="adapter-batch-items" aria-label={t("batch.itemsAria")}>
          {operation.items.map(item => <li key={item.adapter_id} className="adapter-batch-item" data-testid={`batch-item-${item.adapter_id}`}>
            <div className="adapter-batch-item-heading">
              <strong>{item.adapter_name ?? t("batch.unavailableAdapter")}</strong>
              <span className={`adapter-batch-status adapter-batch-status-${item.status}`}>{resultText(item)}</span>
            </div>
            <p>{item.action ? t(`batch.action.${item.action}`) : t("batch.action.unavailable")}</p>
            {item.error_code && <p className="adapter-batch-reason">{reasonText(item.error_code)}</p>}
            {item.executions.length > 0 && <ul className="adapter-batch-executions">
              {item.executions.map(execution => <li key={execution.id}>{t("batch.execution", {
                id: execution.id, status: statusLabel(execution.status),
              })}{execution.cancel_requested && !isTerminal(execution.status) ? ` · ${t("batch.result.cancellationRequested")}` : ""}</li>)}
            </ul>}
            {item.adapter_name !== null && <Button type="link" size="small" disabled={working !== null}
              onClick={() => onInspect(item.adapter_id)}>{t("batch.inspect")}</Button>}
          </li>)}
        </ul>
        <p className="adapter-batch-reference">{t("batch.operationId", { id: operation.id })}</p>
      </>}
    </div>
  </Drawer>;
}
