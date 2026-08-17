/** 执行记录 Tab：游标分页历史列表 + 详情抽屉（M3 §5/§9，M3.2 实时日志/自动打开）。 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Descriptions, Drawer, Empty, Space, Spin, Table, Tabs, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";

import { api } from "../api";
import { useExecutionWatcher } from "../hooks/useExecutionWatcher";
import { isTerminal, statusColor, statusLabel } from "../status";
import type { ExecutionSummary } from "../types";
import { unifiedLogContent } from "../unified-log";
import { userErrorMessage } from "../user-message";
import { LogView, OutputView } from "./OutputView";

const PAGE_SIZE = 50;

function errorMessage(error: unknown): string {
  return userErrorMessage(error);
}

/** M5.5.10：用户侧触发方式只保留主动/定时/Webhook 三种；历史兼容值原样兜底。 */
function triggerLabel(trigger: string): string {
  if (trigger === "manual" || trigger === "production") {
    return "主动触发";
  }
  if (trigger === "schedule") {
    return "定时触发";
  }
  if (trigger === "webhook") {
    return "Webhook 触发";
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
  /** Server-side filter; Webhook call history excludes legacy manual runs. */
  trigger?: "webhook";
  /** Start 成功后自动打开该 Execution 的详情抽屉（含实时日志）。 */
  autoOpenExecutionId?: number | null;
  recordLabel?: "执行记录" | "调用记录";
}) {
  const [items, setItems] = useState<ExecutionSummary[]>([]);
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [requestedExecutionId, setRequestedExecutionId] = useState<number | null>(null);
  const [selectedSummary, setSelectedSummary] = useState<ExecutionSummary | null>(null);
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
          ...(props.trigger !== undefined ? { trigger: props.trigger } : {}),
        });
        setItems((current) => (beforeId === null ? page.items : [...current, ...page.items]));
        setNextBeforeId(page.next_before_id);
      } catch (error) {
        setLoadError(errorMessage(error));
      } finally {
        setLoading(false);
      }
    },
    [props.adapterId, props.trigger],
  );

  // First page loads on mount (antd Tabs mount lazily, so this only runs
  // after the tab is activated); all state commits happen after the await.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const page = await api.listExecutions(props.adapterId, {
          limit: PAGE_SIZE,
          ...(props.trigger !== undefined ? { trigger: props.trigger } : {}),
        });
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
  }, [props.adapterId, props.trigger]);

  async function openExecution(executionId: number, summary: ExecutionSummary | null = null) {
    const requestId = ++detailRequestRef.current;
    watcher.stop(); // invalidate any previous drawer's stream/polls
    setLoadError(null);
    setRequestedExecutionId(executionId);
    setSelectedSummary(summary);
    setDrawerOpen(true);
    setDetailLoading(true);
    try {
      const loaded = await api.getExecution(executionId);
      if (requestId !== detailRequestRef.current) {
        return; // a newer click or a drawer close invalidated this load
      }
      // watch() commits the detail synchronously and follows non-terminal
      // executions live (SSE + bounded fallback), shared with the Workbench log surface.
      watcher.watch(loaded);
    } catch (error) {
      if (requestId !== detailRequestRef.current) {
        return;
      }
      setLoadError(errorMessage(error));
      setDrawerOpen(false);
      setRequestedExecutionId(null);
      setSelectedSummary(null);
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
      title: "运行节点",
      dataIndex: "worker_name",
      width: 130,
      ellipsis: true,
      render: (name: string | null) => <span title={name ?? undefined}>{name ?? "—"}</span>,
    },
    {
      title: "触发",
      dataIndex: "trigger",
      width: 150,
      render: (trigger: string, summary: ExecutionSummary) => (
        <div>
          <div>{triggerLabel(trigger)}</div>
          {trigger === "schedule" && summary.scheduled_for !== null && (
            <div className="execution-version-debug" data-testid="history-scheduled-for">
              计划 {formatTime(summary.scheduled_for)}
            </div>
          )}
        </div>
      ),
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
      width: 160,
      render: (value: string) => formatTime(value),
    },
  ];
  const activeSummary =
    selectedSummary?.id === requestedExecutionId
      ? selectedSummary
      : (items.find((item) => item.id === requestedExecutionId) ?? null);
  const visibleDetail = detail?.id === requestedExecutionId ? detail : null;

  return (
    <div className="history-panel">
      <Space className="history-toolbar">
        <Button data-testid="history-refresh" loading={loading} onClick={() => void loadPage(null)}>
          刷新
        </Button>
        {loadError && <span className="history-error" role="alert">{loadError}</span>}
      </Space>
      <div className="history-scroll">
        <Table<ExecutionSummary>
          rowKey="id"
          size="small"
          columns={columns}
          dataSource={items}
          pagination={false}
          scroll={{ x: 836 }}
          locale={{ emptyText: <Empty description={`暂无${props.recordLabel ?? "执行记录"}`} /> }}
          onRow={(summary) => ({
            onClick: () => void openExecution(summary.id, summary),
            onKeyDown: (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                void openExecution(summary.id, summary);
              }
            },
            tabIndex: 0,
            "aria-haspopup": "dialog",
            "aria-label": `打开执行详情，运行节点 ${summary.worker_name ?? "未知"}`,
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
        title="执行详情"
        width={640}
        open={drawerOpen}
        onClose={() => {
          detailRequestRef.current += 1; // invalidate any in-flight detail load
          watcher.stop();
          setDrawerOpen(false);
          setRequestedExecutionId(null);
          setSelectedSummary(null);
        }}
      >
        {detailLoading && <Spin />}
        {visibleDetail !== null && !detailLoading && (
          <div className="execution-detail">
            {watcher.fallbackExhausted && !isTerminal(visibleDetail.status) && (
              <Alert
                type="warning"
                showIcon
                message="实时连接已断开，状态可能已过期"
                description="已按权威结果轮询至上限仍未等到终态，请刷新或稍后重新查看该执行。"
              />
            )}
            <Descriptions
              size="small"
              column={2}
              items={[
                { key: "status", label: "状态", children: <Tag color={statusColor(visibleDetail.status)}>{statusLabel(visibleDetail.status)}</Tag> },
                {
                  key: "worker",
                  label: "运行节点",
                  children: activeSummary?.worker_name ? (
                    activeSummary.worker_name
                  ) : visibleDetail.worker_id === null ? (
                    "—"
                  ) : (
                    `运行节点 #${visibleDetail.worker_id}`
                  ),
                },
                { key: "trigger", label: "触发方式", children: triggerLabel(visibleDetail.trigger) },
                ...(visibleDetail.trigger === "schedule"
                  ? [
                      {
                        key: "scheduled-for",
                        label: "计划时间",
                        children: formatTime(visibleDetail.scheduled_for),
                      },
                    ]
                  : []),
                { key: "created", label: "创建时间", children: formatTime(visibleDetail.created_at) },
                { key: "started", label: "开始时间", children: formatTime(visibleDetail.started_at) },
                { key: "ended", label: "结束时间", children: formatTime(visibleDetail.ended_at) },
                { key: "duration", label: "耗时", children: formatDuration(visibleDetail.duration_ms) },
              ]}
            />
            {/* M5.5.10：内部 Execution ID 只作为次级技术信息展示（易理解名称“运行 ID”）。 */}
            <div className="execution-version-debug" data-testid="execution-run-id">
              运行 ID：{visibleDetail.id}
            </div>
            {visibleDetail.error && <pre className="terminal-view error-text" role="alert">{visibleDetail.error}</pre>}
            <Tabs
              size="small"
              items={[
                {
                  key: "input",
                  label: "输入",
                  children: (
                    <pre className="output-view" data-testid="detail-input">
                      {JSON.stringify(visibleDetail.input, null, 2)}
                    </pre>
                  ),
                },
                { key: "output", label: "输出", children: <OutputView execution={visibleDetail} /> },
                {
                  // M5.5.10：stdout/stderr 视图统一为一个按实际顺序的实时日志。
                  key: "log",
                  label: "实时日志",
                  children: (
                    <LogView
                      testId="detail-log"
                      content={unifiedLogContent(watcher.liveStdout, watcher.liveStderr)}
                      truncated={visibleDetail.stdout_truncated || visibleDetail.stderr_truncated}
                    />
                  ),
                },
              ]}
            />
          </div>
        )}
      </Drawer>
    </div>
  );
}
