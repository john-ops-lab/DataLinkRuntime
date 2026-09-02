/** 执行记录 Tab：游标分页历史列表 + 详情抽屉（M3 §5/§9，SSE 自动打开）。 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Descriptions, Drawer, Empty, Space, Spin, Table, Tabs, Tag, Timeline } from "antd";
import { DownOutlined, ReloadOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import { useTranslation } from "react-i18next";

import { api } from "../api";
import { useExecutionWatcher } from "../hooks/useExecutionWatcher";
import { isTerminal, statusColor, statusLabel } from "../status";
import type { ExecutionSummary, ReliableExecutionDetail, ReplayResponse } from "../types";
import { unifiedLogContent } from "../unified-log";
import { userErrorMessage } from "../user-message";
import ExecutionInputSummary from "./ExecutionInputSummary";
import { LogView, OutputView } from "./OutputView";

const PAGE_SIZE = 50;

function errorMessage(error: unknown): string {
  return userErrorMessage(error);
}

/** M5.5.10：用户侧触发方式只保留主动/定时/Webhook 三种；历史兼容值原样兜底。 */
function triggerLabel(
  trigger: string,
  translate: (key: string, options?: Record<string, unknown>) => string,
): string {
  if (trigger === "manual" || trigger === "production") {
    return translate("history.activeTrigger");
  }
  if (trigger === "schedule") {
    return translate("history.scheduledTrigger");
  }
  if (trigger === "webhook") {
    return translate("history.webhookTrigger");
  }
  return translate("history.unknownTrigger", { trigger });
}

function formatTime(value: string | null, locale: "zh-CN" | "en"): string {
  if (value === null) {
    return "—";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale === "en" ? "en-US" : "zh-CN");
}

function formatDuration(
  durationMs: number | null,
  translate: (key: string, options?: Record<string, unknown>) => string,
): string {
  if (durationMs === null) {
    return "—";
  }
  return durationMs >= 1000
    ? translate("units.seconds", { value: (durationMs / 1000).toFixed(1) })
    : translate("units.milliseconds", { value: durationMs });
}

function attemptStatusLabel(
  status: string,
  translate: (key: string, options?: Record<string, unknown>) => string,
): string {
  const knownStatuses = new Set([
    "claimed",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "worker_lost",
    "resource_exceeded",
  ]);
  return knownStatuses.has(status) ? translate(`history.attemptStatus.${status}`) : status;
}

function attemptStatusColor(status: string): string {
  if (status === "succeeded") {
    return "green";
  }
  if (status === "claimed" || status === "running") {
    return "blue";
  }
  if (status === "cancelled") {
    return "orange";
  }
  return "red";
}

function RetryCountdown(props: {
  nextAttemptAt: string;
  translate: (key: string, options?: Record<string, unknown>) => string;
}) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [props.nextAttemptAt]);

  const target = Date.parse(props.nextAttemptAt);
  if (Number.isNaN(target)) {
    return null;
  }
  const seconds = Math.max(0, Math.ceil((target - now) / 1000));
  return (
    <span data-testid="execution-retry-countdown">
      {props.translate("history.retryCountdown", { seconds })}
    </span>
  );
}

export default function ExecutionHistoryPanel(props: {
  adapterId: number;
  /** Server-side filter; Webhook call history excludes legacy manual runs. */
  trigger?: "webhook";
  /** Start 成功后自动打开该 Execution 的详情抽屉（含执行日志）。 */
  autoOpenExecutionId?: number | null;
  /** App has consumed the one-shot auto-open request. */
  onAutoOpenHandled?: () => void;
  recordKind?: "execution" | "call";
}) {
  const { i18n, t } = useTranslation(["runtime", "common"]);
  const locale = i18n.resolvedLanguage === "en" ? "en" : "zh-CN";
  const [items, setItems] = useState<ExecutionSummary[]>([]);
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [requestedExecutionId, setRequestedExecutionId] = useState<number | null>(null);
  const [selectedSummary, setSelectedSummary] = useState<ExecutionSummary | null>(null);
  const [reliableDetail, setReliableDetail] = useState<ReliableExecutionDetail | null>(null);
  const [reliableDetailLoading, setReliableDetailLoading] = useState(false);
  const [reliableDetailError, setReliableDetailError] = useState<string | null>(null);
  const [replayLoading, setReplayLoading] = useState(false);
  const [replayResult, setReplayResult] = useState<ReplayResponse | null>(null);
  // Only the newest detail request may commit UI state: rapid clicks (A slow,
  // B fast) must never let a stale A response overwrite the B detail.
  const detailRequestRef = useRef(0);
  // 详情抽屉的日志与收敛行为与测试运行面板共享同一 hook。
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
    setReliableDetail(null);
    setReliableDetailError(null);
    setReplayResult(null);
    try {
      const loaded = await api.getExecution(executionId);
      if (requestId !== detailRequestRef.current) {
        return; // a newer click or a drawer close invalidated this load
      }
      // watch() commits the detail synchronously and follows non-terminal
      // executions live (SSE + bounded fallback), shared with the Workbench log surface.
      watcher.watch(loaded);
      if (loaded.dispatch_backend === "rabbitmq") {
        setReliableDetailLoading(true);
        void api.getReliableExecutionDetail(executionId).then((runtimeDetail) => {
          if (requestId !== detailRequestRef.current) {
            return;
          }
          setReliableDetail(runtimeDetail);
        }).catch((error: unknown) => {
          if (requestId === detailRequestRef.current) {
            setReliableDetailError(errorMessage(error));
          }
        }).finally(() => {
          if (requestId === detailRequestRef.current) {
            setReliableDetailLoading(false);
          }
        });
      } else {
        setReliableDetailLoading(false);
      }
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
    props.onAutoOpenHandled?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoOpenId]);

  const columns: ColumnsType<ExecutionSummary> = [
    {
      title: t("labels.status", { ns: "common" }),
      dataIndex: "status",
      width: 96,
      render: (status: string) => <Tag color={statusColor(status)}>{statusLabel(status)}</Tag>,
    },
    {
      title: t("labels.runtimeWorker", { ns: "common" }),
      dataIndex: "worker_name",
      width: 130,
      ellipsis: true,
      render: (name: string | null) => <span title={name ?? undefined}>{name ?? "—"}</span>,
    },
    {
      title: t("labels.trigger", { ns: "common" }),
      dataIndex: "trigger",
      width: 150,
      render: (trigger: string, summary: ExecutionSummary) => (
        <div>
          <div>{triggerLabel(trigger, (key, options) => t(key, options))}</div>
          {trigger === "schedule" && summary.scheduled_for !== null && (
            <div className="execution-version-debug" data-testid="history-scheduled-for">
              {t("history.scheduledFor", { time: formatTime(summary.scheduled_for, locale) })}
            </div>
          )}
        </div>
      ),
    },
    {
      title: t("labels.startTime", { ns: "common" }),
      dataIndex: "started_at",
      width: 160,
      render: (value: string | null) => formatTime(value, locale),
    },
    {
      title: t("labels.endTime", { ns: "common" }),
      dataIndex: "ended_at",
      width: 160,
      render: (value: string | null) => formatTime(value, locale),
    },
    {
      title: t("labels.duration", { ns: "common" }),
      dataIndex: "duration_ms",
      width: 100,
      render: (duration: number | null) => formatDuration(duration, (key, options) => t(key, options)),
    },
    {
      title: t("labels.createdTime", { ns: "common" }),
      dataIndex: "created_at",
      width: 160,
      render: (value: string) => formatTime(value, locale),
    },
  ];
  const activeSummary =
    selectedSummary?.id === requestedExecutionId
      ? selectedSummary
      : (items.find((item) => item.id === requestedExecutionId) ?? null);
  const visibleDetail = detail?.id === requestedExecutionId ? detail : null;

  return (
    <div className="history-panel">
      <Space
        className="history-toolbar"
        data-testid="history-toolbar"
        role="toolbar"
        aria-label={t("history.toolbarAria")}
      >
        <Button
          data-testid="history-refresh"
          icon={<ReloadOutlined aria-hidden="true" />}
          aria-label={t("history.refresh")}
          loading={loading}
          onClick={() => void loadPage(null)}
        >
          {t("history.refresh")}
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
          scroll={{ x: 1000 }}
          locale={{
            emptyText: (
              <Empty
                description={props.recordKind === "call" ? t("empty.noCallHistory", { ns: "common" }) : t("empty.noHistory", { ns: "common" })}
              />
            ),
          }}
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
            "aria-label": t("history.openDetail", { worker: summary.worker_name ?? t("labels.unknown", { ns: "common" }) }),
            "data-testid": "history-row",
          })}
        />
      </div>
      {nextBeforeId !== null && (
        <Button
          data-testid="history-load-more"
          icon={<DownOutlined aria-hidden="true" />}
          aria-label={t("history.loadMore")}
          loading={loading}
          onClick={() => void loadPage(nextBeforeId)}
        >
          {t("history.loadMore")}
        </Button>
      )}

      <Drawer
        className="execution-history-drawer"
        title={t("history.detailTitle")}
        width="min(640px, 100vw)"
        keyboard
        open={drawerOpen}
        onClose={() => {
          detailRequestRef.current += 1; // invalidate any in-flight detail load
          watcher.stop();
          setDrawerOpen(false);
          setRequestedExecutionId(null);
          setSelectedSummary(null);
          setReliableDetail(null);
          setReliableDetailError(null);
          setReplayResult(null);
        }}
      >
        {detailLoading && <Spin />}
        {visibleDetail !== null && !detailLoading && (
          <div className="execution-detail">
            {watcher.fallbackExhausted && !isTerminal(visibleDetail.status) && (
              <Alert
                type="warning"
                showIcon
                message={t("history.connectionLost")}
                description={t("history.connectionLostDescription")}
              />
            )}
            {visibleDetail.status === "queued" && (
              <Alert
                type="info"
                showIcon
                data-testid="execution-queued-notice"
                message={t("history.waitingForWorker")}
              />
            )}
            {visibleDetail.status === "retry_wait" && visibleDetail.next_attempt_at !== null && visibleDetail.next_attempt_at !== undefined && (
              <Alert
                type="warning"
                showIcon
                data-testid="execution-retry-notice"
                message={t("history.retryAt", { time: formatTime(visibleDetail.next_attempt_at, locale) })}
                description={(
                  <RetryCountdown
                    nextAttemptAt={visibleDetail.next_attempt_at}
                    translate={(key, options) => t(key, options)}
                  />
                )}
              />
            )}
            <Descriptions
              size="small"
              column={{ xs: 1, sm: 2 }}
              items={[
                { key: "status", label: t("labels.status", { ns: "common" }), children: <Tag color={statusColor(visibleDetail.status)}>{statusLabel(visibleDetail.status)}</Tag> },
                {
                  key: "backend",
                  label: t("history.backend"),
                  children: visibleDetail.dispatch_backend === "rabbitmq"
                    ? t("history.backendRabbitmq")
                    : t("history.backendLegacy"),
                },
                {
                  key: "worker",
                  label: t("labels.runtimeWorker", { ns: "common" }),
                  children: activeSummary?.worker_name ? (
                    activeSummary.worker_name
                  ) : visibleDetail.worker_id === null ? (
                    "—"
                  ) : (
                    `${t("labels.runtimeWorker", { ns: "common" })} #${visibleDetail.worker_id}`
                  ),
                },
                { key: "trigger", label: t("labels.triggerMode", { ns: "common" }), children: triggerLabel(visibleDetail.trigger, (key, options) => t(key, options)) },
                ...(visibleDetail.trigger === "schedule"
                  ? [
                      {
                        key: "scheduled-for",
                        label: t("labels.scheduledTime", { ns: "common" }),
                        children: formatTime(visibleDetail.scheduled_for, locale),
                      },
                    ]
                  : []),
                { key: "created", label: t("labels.createdTime", { ns: "common" }), children: formatTime(visibleDetail.created_at, locale) },
                ...(visibleDetail.queued_at
                  ? [{ key: "queued-at", label: t("history.queuedAt"), children: formatTime(visibleDetail.queued_at, locale) }]
                  : []),
                ...(visibleDetail.status === "retry_wait" && visibleDetail.next_attempt_at
                  ? [{ key: "next-attempt", label: t("history.retryAt", { time: "" }), children: formatTime(visibleDetail.next_attempt_at, locale) }]
                  : []),
                ...(visibleDetail.status === "retry_wait" && visibleDetail.attempt_count !== undefined
                  ? [{
                      key: "attempts",
                      label: t("history.attempts", {
                        current: visibleDetail.attempt_count,
                        max: visibleDetail.max_attempts_snapshot ?? "—",
                      }),
                      children: "",
                    }]
                  : []),
                ...((visibleDetail.last_error_code ?? visibleDetail.error_code)
                  ? [{
                      key: "stable-error",
                      label: t("history.stableError", { code: visibleDetail.last_error_code ?? visibleDetail.error_code }),
                      children: "",
                    }]
                  : []),
                { key: "started", label: t("labels.startTime", { ns: "common" }), children: formatTime(visibleDetail.started_at, locale) },
                { key: "ended", label: t("labels.endTime", { ns: "common" }), children: formatTime(visibleDetail.ended_at, locale) },
                { key: "duration", label: t("labels.duration", { ns: "common" }), children: formatDuration(visibleDetail.duration_ms, (key, options) => t(key, options)) },
              ]}
            />
            <Space direction="vertical" size="small" className="execution-detail-runtime-facts">
              {visibleDetail.dispatch_backend === "rabbitmq" && (
                <div className="reliable-runtime-facts" data-testid="execution-reliable-runtime-facts">
                  <div className="reliable-runtime-section">
                    <div className="reliable-runtime-section-title">{t("history.runtimeFacts")}</div>
                    {reliableDetailLoading && <Spin size="small" />}
                    {reliableDetailError !== null && (
                      <Alert
                        type="warning"
                        showIcon
                        data-testid="execution-reliable-detail-error"
                        message={t("history.reliableDetailUnavailable")}
                        description={reliableDetailError}
                      />
                    )}
                  </div>
                  {reliableDetail !== null && (
                    <>
                      <div className="reliable-runtime-section" data-testid="execution-attempt-timeline">
                        <div className="reliable-runtime-section-title">{t("history.attemptTimeline")}</div>
                        {reliableDetail.attempts.length === 0 ? (
                          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("history.noAttempts")} />
                        ) : (
                          <Timeline
                            items={reliableDetail.attempts.map((attempt) => ({
                              key: String(attempt.id),
                              color: attemptStatusColor(attempt.status),
                              children: (
                                <div data-testid={`execution-attempt-${attempt.attempt_no}`}>
                                  <div className="reliable-runtime-attempt-heading">
                                    <strong>{t("history.attemptNumber", { number: attempt.attempt_no })}</strong>
                                    <Tag color={attemptStatusColor(attempt.status)}>
                                      {attemptStatusLabel(attempt.status, (key, options) => t(key, options))}
                                    </Tag>
                                  </div>
                                  <div className="execution-version-debug">
                                    {t("history.attemptClaimedAt", { time: formatTime(attempt.claimed_at, locale) })}
                                  </div>
                                  {attempt.error_code !== null && (
                                    <div className="execution-version-debug">
                                      {t("history.attemptError", { code: attempt.error_code })}
                                    </div>
                                  )}
                                </div>
                              ),
                            }))}
                          />
                        )}
                      </div>
                      <div className="reliable-runtime-section" data-testid="execution-incidents">
                        <div className="reliable-runtime-section-title">{t("history.incidents")}</div>
                        {reliableDetail.incidents.length === 0 ? (
                          <div className="execution-version-debug">{t("history.noIncidents")}</div>
                        ) : (
                          reliableDetail.incidents.map((incident) => (
                            <Alert
                              key={incident.id}
                              type={incident.status === "open" ? "warning" : "info"}
                              showIcon
                              message={t("history.incidentTitle", { kind: incident.kind })}
                              description={t("history.incidentAttempts", {
                                attempts: incident.attempts,
                                error: incident.last_error ?? "—",
                              })}
                            />
                          ))
                        )}
                      </div>
                    </>
                  )}
                </div>
              )}
              {visibleDetail.status === "dead_letter" && (
                reliableDetail?.replay_available === true
                  ? (
                    <>
                      <Button
                        type="primary"
                        loading={replayLoading}
                        data-testid="execution-replay"
                        onClick={() => {
                          if (requestedExecutionId === null) {
                            return;
                          }
                          setReplayLoading(true);
                          void api.replayExecution(requestedExecutionId).then((result) => {
                            setReplayResult(result);
                          }).catch((error: unknown) => {
                            setLoadError(errorMessage(error));
                          }).finally(() => setReplayLoading(false));
                        }}
                      >
                        {t("history.replay")}
                      </Button>
                      {replayResult !== null && (
                        <Alert
                          type="success"
                          showIcon
                          data-testid="execution-replay-success"
                          message={t("history.replayCreated", { id: replayResult.execution_id })}
                          action={(
                            <Button size="small" onClick={() => void openExecution(replayResult.execution_id)}>
                              {t("history.openReplay")}
                            </Button>
                          )}
                        />
                      )}
                    </>
                  )
                  : (reliableDetail?.replay_reason === "dead_letter_input_expired" || visibleDetail.replay_unavailable_reason === "dead_letter_input_expired")
                    ? <Alert type="warning" showIcon data-testid="execution-replay-expired" message={t("history.replayExpired")} />
                    : <Button disabled data-testid="execution-replay-unavailable">{t("history.replayUnavailable")}</Button>
              )}
            </Space>
            {/* M5.5.10：内部 Execution ID 只作为次级技术信息展示（易理解名称“运行 ID”）。 */}
            <div className="execution-version-debug" data-testid="execution-run-id">
              {t("history.runId", { id: visibleDetail.id })}
            </div>
            <Tabs
              className="execution-detail-tabs"
              size="small"
              items={[
                {
                  key: "input",
                  label: t("labels.input", { ns: "common" }),
                  children: <ExecutionInputSummary execution={visibleDetail} />,
                },
                { key: "output", label: t("labels.output", { ns: "common" }), children: <OutputView execution={visibleDetail} /> },
                {
                  // M5.5.10：stdout/stderr 视图统一为一个按实际顺序的执行日志。
                  key: "log",
                  label: t("labels.executionLog", { ns: "common" }),
                  children: (
                    <LogView
                      testId="detail-log"
                      content={unifiedLogContent(visibleDetail.stdout, visibleDetail.stderr, visibleDetail.error)}
                      truncated={visibleDetail.stdout_truncated || visibleDetail.stderr_truncated}
                      downloadFileName={`execution-${visibleDetail.id}`}
                      mode="history"
                      followControls={false}
                      allowDownload
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
