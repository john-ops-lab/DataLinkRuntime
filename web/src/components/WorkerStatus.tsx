/** Worker execution-readiness details for the administrator System Status page. */

import { Button, Empty, List, Spin, Tag, Typography } from "antd";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  isWorkerExecutionReady,
  workerStatusLevel,
  REQUIRED_ISOLATION_CAPABILITIES,
  type RequiredIsolationCapability,
} from "../system-status";
import type { Adapter, Worker } from "../types";
import WorkerCachePanel from "./WorkerCachePanel";
import WorkerMaintenanceObservation from "./WorkerMaintenanceObservation";
import WorkerMaintenanceActions from "./WorkerMaintenanceActions";

function formatTime(value: string, locale: "zh-CN" | "en"): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale === "en" ? "en-US" : "zh-CN");
}

function isolationCapabilityLabel(
  capability: RequiredIsolationCapability,
  translate: (key: string) => string,
): string {
  return translate(`worker.capability.${capability}`);
}

interface WorkerStatusProps {
  adapters?: Adapter[];
  workers: Worker[];
  loading: boolean;
  error: string | null;
  canManageCache?: boolean;
  onRefresh?: () => Promise<void>;
  onReadMaintenance?: (workerId: number) => Promise<import("../types").WorkerMaintenanceSnapshot>;
}

export default function WorkerStatus({ workers, loading, error, canManageCache = false, adapters, onRefresh, onReadMaintenance }: WorkerStatusProps) {
  const { i18n, t } = useTranslation(["common", "settings"]);
  const [cacheWorker, setCacheWorker] = useState<Worker | null>(null);
  const locale = i18n.resolvedLanguage === "en" ? "en" : "zh-CN";
  const remote = workers.some((worker) => worker.protocol_version === 4);
  const readyCount = workers.filter((worker) => workerStatusLevel(worker) === "normal").length;

  return (
    <div className="worker-status-details" data-testid="worker-status-details">
      {loading && (
        <div className="system-status-loading" role="status" aria-label={t("worker.loading")}>
          <Spin size="small" />
          <Typography.Text type="secondary">{t("worker.loading")}</Typography.Text>
        </div>
      )}
      {error !== null && <span className="history-error" role="alert">{error}</span>}
      {!loading && error === null && workers.length === 0 && (
        <Empty description={t("worker.empty")} />
      )}
      {!loading && workers.length > 0 && (
        <>
          <Typography.Text type="secondary" className="worker-execution-summary">
            {t(remote ? "worker.maintenance.summary" : "worker.executionSummary", { ready: readyCount, total: workers.length })}
          </Typography.Text>
          {workers.filter((worker) => worker.protocol_version === 4).length > 2 && (
            <p className="worker-hint" role="alert">{t("worker.fixedNodeLimit")}</p>
          )}
          <List
            size="small"
            dataSource={workers}
            renderItem={(worker) => {
              const executionReady = isWorkerExecutionReady(worker);
              const remoteWorker = worker.protocol_version === 4;
              const cacheSupported = !remoteWorker || worker.isolation_capabilities?.cache_governance_v1 === true;
              const level = workerStatusLevel(worker);
              return (
                <List.Item
                  key={worker.id}
                  data-testid="worker-item"
                  actions={canManageCache && cacheSupported ? [
                    <Button key="cache" size="small" onClick={() => setCacheWorker(worker)}>
                      {t("worker.cacheManagement")}
                    </Button>,
                  ] : undefined}
                >
                  <List.Item.Meta
                    title={(
                      <span className="worker-title-line">
                        <span>{worker.name}</span>
                        <Tag color={worker.status === "online" ? "green" : "red"}>
                          {worker.status === "online" ? t("worker.online") : t("worker.offline")}
                        </Tag>
                        <Tag color={remoteWorker ? (level === "normal" ? "green" : level === "checking" ? "default" : "gold") : executionReady ? "green" : "red"}>
                          {remoteWorker ? t(level === "normal" ? "worker.maintenance.observedNormal" : level === "checking" ? "worker.unknownStatus" : "worker.maintenance.observedRestricted")
                            : executionReady ? t("worker.executionReady") : t("worker.executionUnavailable")}
                        </Tag>
                        {remoteWorker && <Tag>{t("worker.maintenance.remotePython")}</Tag>}
                        {remoteWorker && worker.capabilities.length > 0 && (
                          <Tag>{t("worker.languages", { languages: worker.capabilities.join(", ") })}</Tag>
                        )}
                      </span>
                    )}
                    description={remoteWorker ? <>
                      <WorkerMaintenanceObservation worker={worker} />
                      {canManageCache && onRefresh && onReadMaintenance && worker.maintenance && <WorkerMaintenanceActions
                        key={`${worker.maintenance.scope}:${worker.id}:${worker.maintenance.snapshot?.worker_identity_id ?? "unknown"}`}
                        worker={worker} onRefresh={onRefresh} onRead={onReadMaintenance} />}
                    </> : (
                      <details className="admin-worker-checks" open={!executionReady}><summary>{t("systemStatus.environmentDetails", { ns: "settings" })}</summary><div className="worker-facts">
                        <div>{t("worker.lastHeartbeat", { time: formatTime(worker.last_heartbeat, locale) })}</div>
                        {worker.isolation_preflight_at && (
                          <div>{t("worker.isolationPreflightAt", { time: formatTime(worker.isolation_preflight_at, locale) })}</div>
                        )}
                        <div className="worker-fact-tags">
                          <Tag color={worker.protocol_version === 3 ? "green" : "red"}>
                            {worker.protocol_version === 3
                              ? t("worker.executionInterfaceCompatible")
                              : t("worker.executionInterfaceIncompatible")}
                          </Tag>
                          <Tag color={worker.isolation_preflight_status === "passed" ? "green" : "red"}>
                            {worker.isolation_preflight_status === "passed"
                              ? t("worker.isolationPreflightPassed")
                              : worker.isolation_preflight_status === "failed"
                                ? t("worker.isolationPreflightFailed")
                                : t("worker.isolationPreflightUnknown")}
                          </Tag>
                          <Tag color={worker.rabbitmq_execution_v3 === true ? "green" : "red"}>
                            {worker.rabbitmq_execution_v3 === true ? t("worker.dispatchReady") : t("worker.dispatchUnavailable")}
                          </Tag>
                          <Tag>{t("worker.languages", { languages: worker.capabilities.join(", ") || "—" })}</Tag>
                        </div>
                        <div className="worker-capabilities">
                          {REQUIRED_ISOLATION_CAPABILITIES.map((capability) => {
                            const enabled = worker.isolation_capabilities?.[capability] === true;
                            return (
                              <Tag key={capability} color={enabled ? "green" : "red"}>
                                {enabled ? "✓" : "×"} {isolationCapabilityLabel(capability, (key) => t(key))}
                              </Tag>
                            );
                          })}
                        </div>
                      </div></details>
                    )}
                  />
                </List.Item>
              );
            }}
          />
        </>
      )}
      {!loading && (
        <p className="worker-hint">
          {t("worker.hint")}
        </p>
      )}
      {cacheWorker !== null && (
        <WorkerCachePanel
          worker={cacheWorker}
          adapters={adapters}
          open
          onClose={() => setCacheWorker(null)}
        />
      )}
    </div>
  );
}
