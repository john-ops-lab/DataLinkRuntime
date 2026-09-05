/** Worker execution-readiness details for the administrator System Status page. */

import { Empty, List, Spin, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  isWorkerExecutionReady,
  REQUIRED_ISOLATION_CAPABILITIES,
  type RequiredIsolationCapability,
} from "../system-status";
import type { Worker } from "../types";

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
  workers: Worker[];
  loading: boolean;
  error: string | null;
}

export default function WorkerStatus({ workers, loading, error }: WorkerStatusProps) {
  const { i18n, t } = useTranslation("common");
  const locale = i18n.resolvedLanguage === "en" ? "en" : "zh-CN";
  const readyCount = workers.filter(isWorkerExecutionReady).length;

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
            {t("worker.executionSummary", { ready: readyCount, total: workers.length })}
          </Typography.Text>
          <List
            size="small"
            dataSource={workers}
            renderItem={(worker) => {
              const executionReady = isWorkerExecutionReady(worker);
              return (
                <List.Item key={worker.id} data-testid="worker-item">
                  <List.Item.Meta
                    title={(
                      <span className="worker-title-line">
                        <span>{worker.name}</span>
                        <Tag color={worker.status === "online" ? "green" : "red"}>
                          {worker.status === "online" ? t("worker.online") : t("worker.offline")}
                        </Tag>
                        <Tag color={executionReady ? "green" : "red"}>
                          {executionReady ? t("worker.executionReady") : t("worker.executionUnavailable")}
                        </Tag>
                      </span>
                    )}
                    description={(
                      <div className="worker-facts">
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
                      </div>
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
    </div>
  );
}
