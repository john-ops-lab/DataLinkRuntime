/** Read-only facts; maintenance actions have their own explicit intent flow. */
import { useTranslation } from "react-i18next";

import type { Worker } from "../types";

export default function WorkerMaintenanceObservation({ worker }: { worker: Worker }) {
  const { t, i18n } = useTranslation("common");
  const view = worker.maintenance;
  const snapshot = view?.snapshot;
  const format = (value: string | null) => {
    if (value === null) return "—";
    return new Date(value).toLocaleString(i18n.resolvedLanguage === "en" ? "en-US" : "zh-CN");
  };
  return (
    <div className="worker-maintenance" data-testid="worker-v4-observation">
      <p className="worker-maintenance-hint">{t("worker.maintenance.observationOnly")}</p>
      {(!view?.fresh || view.error !== null) && (
        <p role="status" className="worker-maintenance-notice">
          {t(view?.error === "unsupported" ? "worker.maintenance.matchingVersion" : "worker.maintenance.stale")}
        </p>
      )}
      {snapshot ? (
        <>
          <dl className="worker-maintenance-facts">
            <dt>{t("worker.maintenance.identity")}</dt><dd><code>{snapshot.worker_identity_id}</code></dd>
            <dt>{t("worker.maintenance.instance")}</dt><dd><code>{snapshot.instance_id ?? "—"}</code> / {snapshot.instance_generation}</dd>
            <dt>{t("worker.maintenance.observedAt")}</dt><dd>{format(snapshot.observed_at)}</dd>
            <dt>{t("worker.maintenance.observedOnline")}</dt><dd>{t(snapshot.online ? "worker.online" : "worker.offline")}</dd>
            <dt>{t("worker.maintenance.scheduling")}</dt><dd>{t(`worker.maintenance.state.${snapshot.scheduling_state}`)}</dd>
            <dt>{t("worker.maintenance.capacity")}</dt><dd>{t(`worker.maintenance.capacityState.${snapshot.capacity_state}`)}</dd>
            <dt>{t("worker.maintenance.version")}</dt><dd data-testid="worker-maintenance-version">{snapshot.maintenance_version}</dd>
            <dt>{t("worker.maintenance.nominalLease")}</dt><dd>{format(snapshot.instance_lease_until)}</dd>
            <dt>{t("worker.maintenance.preflight")}</dt><dd>{t(`worker.maintenance.preflightState.${snapshot.isolation_preflight_status}`)} · {format(snapshot.isolation_preflight_at)}</dd>
            <dt>{t("worker.maintenance.running")}</dt><dd>{snapshot.active_attempts}</dd>
            <dt>{t("worker.maintenance.unsettled")}</dt><dd>{snapshot.unsettled_attempts}</dd>
            <dt>{t("worker.maintenance.reconciliation")}</dt><dd>{t(snapshot.has_unreconciled_attempts ? "worker.maintenance.required" : "worker.maintenance.noHistoryBlock")}</dd>
          </dl>
          {snapshot.last_recovery_disposition && (
            <div className="worker-maintenance-notice" data-testid="worker-recovery-disposition">
              <strong>{t("worker.maintenance.resultUnknown")}</strong>
              <p>{t("worker.maintenance.settlementOnly")}</p>
              <p>{t("worker.maintenance.originalState", {
                execution: snapshot.last_recovery_disposition.execution_id,
                executionStatus: snapshot.last_recovery_disposition.execution_status,
                attempt: snapshot.last_recovery_disposition.attempt_id,
                attemptStatus: snapshot.last_recovery_disposition.attempt_status,
              })}</p>
              <small>{t("worker.maintenance.disposition")}: <code>{snapshot.last_recovery_disposition.disposition_id}</code></small>
            </div>
          )}
        </>
      ) : <p>{t("worker.maintenance.noObservation")}</p>}
      <p className="worker-maintenance-hint">{t("worker.maintenance.physicalBoundary")}</p>
      <p className="worker-maintenance-links">
        <a href="https://github.com/john-ops-lab/DataLinkRuntime/blob/main/docs/zh-CN/remote-worker-wave1b.md" target="_blank" rel="noreferrer">{t("worker.maintenance.recoveryGuide")}</a>
        {" · "}
        <a href="https://github.com/john-ops-lab/DataLinkRuntime/blob/main/docs/zh-CN/remote-worker-wave1c.md" target="_blank" rel="noreferrer">{t("worker.maintenance.unknownGuide")}</a>
      </p>
    </div>
  );
}
