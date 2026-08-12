/** 顶部 Worker 状态观察（M3 §10）：复用 App 为 Catalog/设置加载的列表。 */

import { Badge, Button, Empty, List, Popover, Spin, Tag } from "antd";

import type { Worker } from "../types";

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

interface WorkerStatusProps {
  workers: Worker[];
  loading: boolean;
  error: string | null;
}

export default function WorkerStatus({ workers, loading, error }: WorkerStatusProps) {

  const content = (
    <div className="worker-popover">
      {loading && <Spin size="small" />}
      {error !== null && <span className="history-error">{error}</span>}
      {!loading && error === null && workers.length === 0 && (
        <Empty description="暂无已注册 Worker" />
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
                    <Tag color={worker.status === "online" ? "green" : "red"}>{worker.status}</Tag>
                  </span>
                }
                description={`最近心跳：${formatTime(worker.last_heartbeat)}`}
              />
            </List.Item>
          )}
        />
      )}
      {!loading && (
        <p className="worker-hint">
          状态为 Worker 最近上报值，平台不做心跳超时判定，请结合最近心跳时间判断 Worker 是否可能离线。
        </p>
      )}
    </div>
  );

  const hasOnlineWorker = workers.some((worker) => worker.status === "online");
  const allOffline = workers.length > 0 && !hasOnlineWorker;

  return (
    <Popover
      content={content}
      title="Workers"
      trigger="click"
    >
      <Button size="small" data-testid="worker-status">
        <Badge status={hasOnlineWorker ? "success" : allOffline ? "error" : "default"} />
        Workers
      </Button>
    </Popover>
  );
}
