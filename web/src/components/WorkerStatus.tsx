/** 顶部 Worker 状态观察（M3 §10）：复用 App 为 Catalog/设置加载的列表。 */

import { Badge, Button, Empty, List, Popover, Spin, Tag } from "antd";
import { useTranslation } from "react-i18next";

import type { Worker } from "../types";

function formatTime(value: string, locale: "zh-CN" | "en"): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale === "en" ? "en-US" : "zh-CN");
}

interface WorkerStatusProps {
  workers: Worker[];
  loading: boolean;
  error: string | null;
}

export default function WorkerStatus({ workers, loading, error }: WorkerStatusProps) {
  const { i18n, t } = useTranslation("common");
  const locale = i18n.resolvedLanguage === "en" ? "en" : "zh-CN";
  const content = (
    <div className="worker-popover">
      {loading && <Spin size="small" />}
      {error !== null && <span className="history-error" role="alert">{error}</span>}
      {!loading && error === null && workers.length === 0 && (
        <Empty description={t("worker.empty")} />
      )}
      {!loading && workers.length > 0 && (
        <List
          size="small"
          dataSource={workers}
          renderItem={(worker) => (
            <List.Item key={worker.id} data-testid="worker-item">
              <List.Item.Meta
                title={
                  <span>
                    {worker.name}{" "}
                    <Tag color={worker.status === "online" ? "green" : "red"}>
                      {worker.status === "online" ? t("worker.online") : t("worker.offline")}
                    </Tag>
                  </span>
                }
                description={t("worker.lastHeartbeat", { time: formatTime(worker.last_heartbeat, locale) })}
              />
            </List.Item>
          )}
        />
      )}
      {!loading && (
        <p className="worker-hint">
          {t("worker.hint")}
        </p>
      )}
    </div>
  );

  const hasOnlineWorker = workers.some((worker) => worker.status === "online");
  const onlineCount = workers.filter((worker) => worker.status === "online").length;
  const allOffline = workers.length > 0 && !hasOnlineWorker;

  return (
    <Popover
      content={content}
      title={t("worker.title")}
      trigger="click"
    >
      <Button size="small" data-testid="worker-status">
        <Badge status={hasOnlineWorker ? "success" : allOffline ? "error" : "default"} />
        {t("worker.title")} · {loading ? t("worker.loading") : error !== null ? t("worker.unknownStatus") : t("worker.onlineSummary", { online: onlineCount, total: workers.length })}
      </Button>
    </Popover>
  );
}
