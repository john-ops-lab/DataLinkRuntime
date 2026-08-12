/** 执行记录 Tab：游标分页历史列表 + 详情抽屉（M3 §5/§9，M3.2 实时日志/自动打开）。 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Descriptions, Drawer, Empty, Space, Spin, Table, Tabs, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";

import { ApiError, api } from "../api";
import { useExecutionWatcher } from "../hooks/useExecutionWatcher";
import { isTerminal, statusColor, statusLabel } from "../status";
import type { ExecutionSummary } from "../types";
import { LogView, OutputView } from "./OutputView";

const PAGE_SIZE = 50;

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "请求失败";
}

/** M3.2：触发列区分测试运行与生产启动；未知值原样展示。 */
function triggerLabel(trigger: string): string {
  if (trigger === "manual") {
    return "测试运行";
  }
  if (trigger === "production") {
    return "生产启动";
  }
  return trigger;
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

export default function ExecutionHistoryPanel(props: {
  adapterId: number;
  /** Start 成功后自动打开该 Execution 的详情抽屉（含实时日志）。 */
  autoOpenExecutionId?: number | null;
}) {
  const [items, setItems] = useState<ExecutionSummary[]>([]);
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // Only the newest detail request may commit UI state: rapid clicks (A slow,
  // B fast) must never let a stale A response overwrite the B detail.
  const detailRequestRef = useRef(0);
  // 详情抽屉的实时日志与收敛行为与测试运行面板共享同一 hook。
  const watcher = useExecutionWatcher(setLoadError);
  const detail = watcher.execution;

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

  async function openExecution(executionId: number) {
    const requestId = ++detailRequestRef.current;
    watcher.stop(); // invalidate any previous drawer's stream/polls
    setDrawerOpen(true);
    setDetailLoading(true);
    try {
      const loaded = await api.getExecution(executionId);
      if (requestId !== detailRequestRef.current) {
        return; // a newer click or a drawer close invalidated this load
      }
      // watch() commits the detail synchronously and follows non-terminal
      // executions live (SSE + bounded fallback), shared with TestRunPanel.
      watcher.watch(loaded);
    } catch (error) {
      if (requestId !== detailRequestRef.current) {
        return;
      }
      setLoadError(errorMessage(error));
      setDrawerOpen(false);
    } finally {
      if (requestId === detailRequestRef.current) {
        setDetailLoading(false);
      }
    }
  }

  // Start 成功后由 App 切到本 Tab 并传入新 Execution id：首次挂载（antd Tabs
  // 懒加载）或 id 变化时自动打开详情抽屉。openExecution 的 setState 是这条
  // 自动打开路径的有意同步副作用（与行点击入口共用同一函数）。
  const autoOpenId = props.autoOpenExecutionId ?? null;
  useEffect(() => {
    if (autoOpenId === null) {
      return;
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 自动打开抽屉的同步 setState 是有意的（与行点击共用 openExecution）
    void openExecution(autoOpenId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoOpenId]);

  const columns: ColumnsType<ExecutionSummary> = [
    {
      title: "状态",
      dataIndex: "status",
      width: 96,
      render: (status: string) => <Tag color={statusColor(status)}>{statusLabel(status)}</Tag>,
    },
    {
      title: "Execution",
      dataIndex: "id",
      width: 110,
      render: (id: number) => `#${id}`,
    },
    {
      title: "Version",
      dataIndex: "version_seq",
      width: 90,
      render: (seq: number) => `v${seq}`,
    },
    {
      title: "Worker",
      dataIndex: "worker_name",
      width: 130,
      render: (name: string | null) => name ?? "—",
    },
    {
      title: "触发",
      dataIndex: "trigger",
      width: 90,
      render: (trigger: string) => triggerLabel(trigger),
    },
    {
      title: "耗时",
      dataIndex: "duration_ms",
      width: 100,
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
      <div className="history-scroll">
        <Table<ExecutionSummary>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={items}
          pagination={false}
          locale={{ emptyText: <Empty description="暂无执行记录" /> }}
          onRow={(summary) => ({
            onClick: () => void openExecution(summary.id),
            "data-testid": "history-row",
          })}
        />
      </div>
      {nextBeforeId !== null && (
        <Button
          data-testid="history-load-more"
          loading={loading}
          onClick={() => void loadPage(nextBeforeId)}
        >
          加载更多
        </Button>
      )}

      <Drawer
        title={detail !== null ? `Execution #${detail.id}` : "执行详情"}
        width={560}
        open={drawerOpen}
        onClose={() => {
          detailRequestRef.current += 1; // invalidate any in-flight detail load
          watcher.stop();
          setDrawerOpen(false);
        }}
      >
        {detailLoading && <Spin />}
        {detail !== null && (
          <div className="execution-detail">
            {watcher.fallbackExhausted && !isTerminal(detail.status) && (
              <Alert
                type="warning"
                showIcon
                message="实时连接已断开，状态可能已过期"
                description="已按权威结果轮询至上限仍未等到终态，请刷新或稍后重新查看该 Execution。"
              />
            )}
            <Descriptions
              size="small"
              column={2}
              items={[
                { key: "status", label: "状态", children: <Tag color={statusColor(detail.status)}>{statusLabel(detail.status)}</Tag> },
                { key: "version", label: "Version ID", children: detail.version_id },
                { key: "worker", label: "Worker", children: detail.worker_id ?? "—" },
                { key: "trigger", label: "触发方式", children: triggerLabel(detail.trigger) },
                { key: "created", label: "创建时间", children: formatTime(detail.created_at) },
                { key: "started", label: "开始时间", children: formatTime(detail.started_at) },
                { key: "ended", label: "结束时间", children: formatTime(detail.ended_at) },
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
                  children: <LogView testId="detail-stdout" content={watcher.liveStdout} truncated={detail.stdout_truncated} />,
                },
                {
                  key: "stderr",
                  label: "stderr",
                  children: <LogView testId="detail-stderr" content={watcher.liveStderr} truncated={detail.stderr_truncated} />,
                },
              ]}
            />
          </div>
        )}
      </Drawer>
    </div>
  );
}
