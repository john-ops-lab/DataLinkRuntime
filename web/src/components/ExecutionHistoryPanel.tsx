/** 执行记录 Tab：游标分页历史列表 + 详情抽屉（M3 §5/§9）。 */

import { useCallback, useEffect, useState } from "react";
import { Button, Descriptions, Drawer, Empty, Space, Spin, Table, Tabs, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";

import { ApiError, api } from "../api";
import { statusColor, statusLabel } from "../status";
import type { Execution, ExecutionSummary } from "../types";
import { LogView, OutputView } from "./OutputView";

const PAGE_SIZE = 50;

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "请求失败";
}

function formatTime(value: string | null): string {
  if (value === null) {
    return "—";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatDuration(durationMs: number | null): string {
  if (durationMs === null) {
    return "—";
  }
  return durationMs >= 1000 ? `${(durationMs / 1000).toFixed(1)} 秒` : `${durationMs} 毫秒`;
}

export default function ExecutionHistoryPanel(props: { adapterId: number }) {
  const [items, setItems] = useState<ExecutionSummary[]>([]);
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [detail, setDetail] = useState<Execution | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const loadPage = useCallback(
    async (beforeId: number | null) => {
      setLoading(true);
      setLoadError(null);
      try {
        const page = await api.listExecutions(props.adapterId, {
          limit: PAGE_SIZE,
          ...(beforeId !== null ? { before_id: beforeId } : {}),
        });
        setItems((current) => (beforeId === null ? page.items : [...current, ...page.items]));
        setNextBeforeId(page.next_before_id);
      } catch (error) {
        setLoadError(errorMessage(error));
      } finally {
        setLoading(false);
      }
    },
    [props.adapterId],
  );

  // First page loads on mount (antd Tabs mount lazily, so this only runs
  // after the tab is activated); all state commits happen after the await.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const page = await api.listExecutions(props.adapterId, { limit: PAGE_SIZE });
        if (cancelled) {
          return;
        }
        setItems(page.items);
        setNextBeforeId(page.next_before_id);
      } catch (error) {
        if (!cancelled) {
          setLoadError(errorMessage(error));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [props.adapterId]);

  async function openDetail(summary: ExecutionSummary) {
    setDrawerOpen(true);
    setDetail(null);
    setDetailLoading(true);
    try {
      setDetail(await api.getExecution(summary.id));
    } catch (error) {
      setLoadError(errorMessage(error));
      setDrawerOpen(false);
    } finally {
      setDetailLoading(false);
    }
  }

  const columns: ColumnsType<ExecutionSummary> = [
    { title: "ID", dataIndex: "id", width: 80 },
    {
      title: "状态",
      dataIndex: "status",
      width: 100,
      render: (status: string) => <Tag color={statusColor(status)}>{statusLabel(status)}</Tag>,
    },
    {
      title: "版本",
      dataIndex: "version_seq",
      width: 80,
      render: (seq: number) => `v${seq}`,
    },
    {
      title: "Worker",
      dataIndex: "worker_name",
      width: 140,
      render: (name: string | null) => name ?? "—",
    },
    {
      title: "耗时",
      dataIndex: "duration_ms",
      width: 110,
      render: (duration: number | null) => formatDuration(duration),
    },
    {
      title: "创建时间",
      dataIndex: "created_at",
      render: (value: string) => formatTime(value),
    },
  ];

  return (
    <div className="history-panel">
      <Space className="history-toolbar">
        <Button data-testid="history-refresh" loading={loading} onClick={() => void loadPage(null)}>
          刷新
        </Button>
        {loadError && <span className="history-error">{loadError}</span>}
      </Space>
      <Table<ExecutionSummary>
        rowKey="id"
        size="small"
        columns={columns}
        dataSource={items}
        pagination={false}
        locale={{ emptyText: <Empty description="暂无执行记录" /> }}
        onRow={(summary) => ({
          onClick: () => void openDetail(summary),
          "data-testid": "history-row",
        })}
      />
      {nextBeforeId !== null && (
        <Button
          data-testid="history-load-more"
          loading={loading}
          onClick={() => void loadPage(nextBeforeId)}
        >
          加载更多
        </Button>
      )}

      <Drawer title={detail !== null ? `Execution #${detail.id}` : "执行详情"} width={560} open={drawerOpen} onClose={() => setDrawerOpen(false)}>
        {detailLoading && <Spin />}
        {detail !== null && (
          <div className="execution-detail">
            <Descriptions
              size="small"
              column={2}
              items={[
                { key: "status", label: "状态", children: <Tag color={statusColor(detail.status)}>{statusLabel(detail.status)}</Tag> },
                { key: "version", label: "Version ID", children: detail.version_id },
                { key: "worker", label: "Worker", children: detail.worker_id ?? "—" },
                { key: "trigger", label: "触发方式", children: detail.trigger },
                { key: "created", label: "创建时间", children: formatTime(detail.created_at) },
                { key: "duration", label: "耗时", children: formatDuration(detail.duration_ms) },
              ]}
            />
            {detail.error && <pre className="terminal-view error-text">{detail.error}</pre>}
            <Tabs
              size="small"
              items={[
                {
                  key: "input",
                  label: "Input",
                  children: (
                    <pre className="output-view" data-testid="detail-input">
                      {JSON.stringify(detail.input, null, 2)}
                    </pre>
                  ),
                },
                { key: "output", label: "Output", children: <OutputView execution={detail} /> },
                {
                  key: "stdout",
                  label: "stdout",
                  children: <LogView testId="detail-stdout" content={detail.stdout} truncated={detail.stdout_truncated} />,
                },
                {
                  key: "stderr",
                  label: "stderr",
                  children: <LogView testId="detail-stderr" content={detail.stderr} truncated={detail.stderr_truncated} />,
                },
              ]}
            />
          </div>
        )}
      </Drawer>
    </div>
  );
}
