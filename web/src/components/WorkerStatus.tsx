/** 顶部 Worker 状态观察（M3 §10）：懒加载，只在打开弹层时请求。 */

import { useState } from "react";
import { Badge, Button, Empty, List, Popover, Spin, Tag } from "antd";

import { ApiError, api } from "../api";
import type { Worker } from "../types";

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "请求失败";
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export default function WorkerStatus() {
  const [open, setOpen] = useState(false);
  const [workers, setWorkers] = useState<Worker[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleOpenChange(nextOpen: boolean) {
    setOpen(nextOpen);
    if (!nextOpen || workers !== null || loading) {
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setWorkers(await api.listWorkers());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }

  const content = (
    <div className="worker-popover">
      {loading && <Spin size="small" />}
      {error !== null && <span className="history-error">{error}</span>}
      {!loading && error === null && workers !== null && workers.length === 0 && (
        <Empty description="暂无已注册 Worker" />
      )}
      {!loading && workers !== null && workers.length > 0 && (
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
      {!loading && workers !== null && (
        <p className="worker-hint">
          状态为 Worker 最近上报值，平台不做心跳超时判定，请结合最近心跳时间判断 Worker 是否可能离线。
        </p>
      )}
    </div>
  );

  return (
    <Popover
      content={content}
      title="Workers"
      trigger="click"
      open={open}
      onOpenChange={(nextOpen) => void handleOpenChange(nextOpen)}
    >
      <Button size="small" data-testid="worker-status">
        <Badge status={workers !== null && workers.length > 0 ? "success" : "default"} />
        Workers
      </Button>
    </Popover>
  );
}
