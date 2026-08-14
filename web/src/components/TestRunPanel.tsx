/** 测试运行 Tab：Input JSON + 固定运行 Latest Revision + 实时日志。 */

import { useEffect, useRef, useState } from "react";
import { Alert, Button, Tabs, Tag, Typography } from "antd";

import { ApiError, api } from "../api";
import { LANGUAGE_LABELS } from "../languages";
import { useExecutionWatcher } from "../hooks/useExecutionWatcher";
import { isTerminal, statusColor, statusLabel } from "../status";
import type { Adapter, Worker } from "../types";
import ActionWithReason from "./ActionWithReason";
import { LogView, OutputView } from "./OutputView";

interface TestRunPanelProps {
  adapter: Adapter;
  runtimeWorker: Worker | null;
  latestVersionSeq: number | null;
  viewingHistoricalVersion: boolean;
  dirty: boolean;
  contentReady: boolean;
  busy: boolean;
  workers: Worker[];
  workersLoading: boolean;
  workersError: string | null;
  onEdit: () => void;
  onOpenSettings: () => void;
  onError: (message: string | null) => void;
  onPublishedVersionTestSucceeded: (adapterId: number) => void;
}

// After any abnormal SSE end, converge on the authoritative M2 result with
// bounded GET polling; the shared behavior lives in useExecutionWatcher.

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "请求失败";
}

function formatDuration(durationMs: number | null): string {
  if (durationMs === null) {
    return "—";
  }
  return durationMs >= 1000 ? `${(durationMs / 1000).toFixed(1)} 秒` : `${durationMs} 毫秒`;
}

export default function TestRunPanel(props: TestRunPanelProps) {
  const {
    adapter,
    onPublishedVersionTestSucceeded,
  } = props;
  const [inputText, setInputText] = useState("{}");
  const [submitting, setSubmitting] = useState(false);
  const watcher = useExecutionWatcher(props.onError);
  const execution = watcher.execution;
  const reportedQualificationExecutionId = useRef<number | null>(null);
  // version_id -> seq resolved locally at run time: the summary must keep
  // showing the version that was actually executed even after the user
  // switches to another version (Review round 1 Minor 1). No extra request;
  // the internal version_id stays as secondary debug info only. Kept as
  // state (not a ref) because it is read during render.
  const [runSeqByVersionId, setRunSeqByVersionId] = useState<Map<number, number>>(new Map());

  useEffect(() => {
    if (
      execution?.status !== "succeeded" ||
      execution.id === reportedQualificationExecutionId.current ||
      execution.version_id !== adapter.published_version_id ||
      (adapter.runtime_worker_id !== null &&
        adapter.runtime_worker_id !== undefined &&
        execution.worker_id !== adapter.runtime_worker_id)
    ) {
      return;
    }
    reportedQualificationExecutionId.current = execution.id;
    onPublishedVersionTestSucceeded(adapter.id);
  }, [
    adapter.id,
    adapter.runtime_worker_id,
    adapter.published_version_id,
    execution,
    onPublishedVersionTestSucceeded,
  ]);

  async function handleRun() {
    // Interaction guards: a saved latest Revision, no dirty runs, valid JSON
    // input and no duplicate submissions.
    if (submitting || props.busy) {
      return;
    }
    if (props.adapter.archived_at) {
      props.onError("该 Adapter 已归档，禁止测试运行，请先在设置中恢复");
      return;
    }
    if (props.dirty) {
      props.onError("当前修改尚未保存，请先保存为新版本后再运行测试");
      return;
    }
    if (!props.contentReady) {
      props.onError("版本内容尚未就绪，请等待加载完成或刷新后重试");
      return;
    }
    if (props.adapter.latest_version_id === null) {
      props.onError("当前 Adapter 还没有已保存的版本，请先在编辑页保存版本");
      return;
    }
    let input: unknown;
    try {
      input = JSON.parse(inputText);
    } catch {
      props.onError("Input 必须是合法 JSON");
      return;
    }
    props.onError(null);
    setSubmitting(true);
    try {
      if (props.latestVersionSeq !== null) {
        const seq = props.latestVersionSeq;
        const versionId = props.adapter.latest_version_id;
        setRunSeqByVersionId((current) => new Map(current).set(versionId, seq));
      }
      const created = await api.createExecution(props.adapter.id, {
        input,
      });
      watcher.watch(created);
    } catch (error) {
      props.onError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  const runBlockedReason = props.adapter.archived_at
    ? "Adapter 已归档，请先在设置中恢复"
    : props.dirty
      ? "存在未保存修改，请先使用顶部“保存新版本”"
      : !props.contentReady
        ? "版本内容尚未就绪，请等待加载完成或刷新后重试"
        : props.adapter.latest_version_id === null
          ? "当前没有已保存版本，请先在编辑页保存为新版本"
          : props.busy || submitting
            ? "其他操作正在进行，请等待完成"
            : null;
  const runDisabled = runBlockedReason !== null;

  // User-facing primary version label is vN; fall back to the internal id
  // only when the seq is genuinely unknown.
  const executionVersionSeq =
    execution === null ? null : (runSeqByVersionId.get(execution.version_id) ?? null);
  const configuredWorkerId = props.adapter.runtime_worker_id;
  const compatibleOnlineWorkers = props.workers.filter(
    (worker) => worker.status === "online" && worker.capabilities.includes(adapter.language),
  );
  const automaticWorker =
    configuredWorkerId === null || configuredWorkerId === undefined
      ? compatibleOnlineWorkers.length === 1
        ? compatibleOnlineWorkers[0]
        : null
      : null;
  const workerContextLabel =
    props.runtimeWorker?.name ??
    (configuredWorkerId === null || configuredWorkerId === undefined
      ? props.workersLoading
        ? "Worker 状态载入中"
        : props.workersError !== null
          ? "Worker 状态暂不可用"
          : automaticWorker !== null
            ? `${automaticWorker.name}（自动）`
            : "未配置运行 Worker"
      : `Worker #${configuredWorkerId}（状态暂不可用）`);
  const workerContextTitle =
    props.runtimeWorker !== null
      ? `${props.runtimeWorker.name} · ${props.runtimeWorker.capabilities.join(" / ")}`
      : configuredWorkerId === null || configuredWorkerId === undefined
        ? automaticWorker !== null
          ? `未固定运行 Worker；后端可自动采用唯一可用 Worker ${automaticWorker.name}`
          : "未固定运行 Worker；只有恰好一个有效在线且兼容的 Worker 时后端才能自动采用"
        : `已配置 Worker #${configuredWorkerId}，当前状态暂不可用`;
  const executionWorker =
    execution?.worker_id === null || execution?.worker_id === undefined
      ? null
      : (props.workers.find((worker) => worker.id === execution.worker_id) ?? null);

  return (
    // M3.1 双栏工作台：左栏测试输入，右栏 Execution 状态与实时日志。
    <div className="test-run-panel">
      <section className="test-input-col" data-testid="test-input-col">
        <h3 className="test-run-col-title">测试输入</h3>
        <div
          className="test-version-row"
          data-testid="test-runtime-info"
          title={workerContextTitle}
        >
          <strong data-testid="test-run-version">
            {props.latestVersionSeq !== null
              ? `Latest v${props.latestVersionSeq}`
              : props.adapter.latest_version_id !== null
                ? `Latest #${props.adapter.latest_version_id}`
                : "未保存版本"}
          </strong>
          <span aria-hidden="true">·</span>
          <span>{LANGUAGE_LABELS[adapter.language]}</span>
          <span aria-hidden="true">·</span>
          <span
            className="test-version-worker"
            title={workerContextLabel}
          >
            {workerContextLabel}
          </span>
          {props.adapter.latest_version_id !== null && <Tag color="blue">固定 Latest</Tag>}
        </div>
        {props.viewingHistoricalVersion && (
          <Typography.Text type="secondary" role="status" data-testid="latest-run-guidance">
            当前正在查看历史 Revision；运行测试仍固定使用 Latest Revision。
          </Typography.Text>
        )}
        {props.runtimeWorker !== null && props.runtimeWorker.status !== "online" && (
          <Typography.Text type="warning" role="status" data-testid="test-worker-offline-warning">
            运行 Worker {props.runtimeWorker.name} 最近状态为离线；可在设置中更换。
            运行时仍由后端按最新心跳复核。
          </Typography.Text>
        )}
        {(configuredWorkerId === null || configuredWorkerId === undefined) &&
          !props.workersLoading &&
          props.workersError === null &&
          automaticWorker === null && (
            <div className="test-blocked-guidance" role="status" data-testid="test-worker-selection-warning">
              <Typography.Text type="warning">
                {compatibleOnlineWorkers.length === 0 ? (
                  <>
                    当前没有有效在线且支持 {LANGUAGE_LABELS[adapter.language]} 的 Worker。
                    请先恢复、启动或注册一个兼容 Worker；运行时仍由后端按最新心跳复核。
                  </>
                ) : (
                  <>
                    当前有 {compatibleOnlineWorkers.length} 个有效在线且兼容的 Worker，无法自动确定。
                    请在设置中指定运行 Worker；运行时仍由后端按最新心跳复核。
                  </>
                )}
              </Typography.Text>
              {compatibleOnlineWorkers.length > 0 && (
                <Button type="link" size="small" onClick={props.onOpenSettings}>
                  打开设置
                </Button>
              )}
            </div>
          )}
        <div className="test-input-block">
          <label className="test-input-label" htmlFor="test-input-json">Input JSON</label>
          <textarea
            id="test-input-json"
            data-testid="test-input"
            className="code-textarea"
            aria-label="测试输入 JSON"
            placeholder="Input（任意合法 JSON）"
            value={inputText}
            disabled={submitting}
            onChange={(event) => setInputText(event.target.value)}
          />
        </div>
        <div className="test-run-actions">
          <ActionWithReason label="运行测试" reason={runBlockedReason}>
            <Button
              type="primary"
              data-testid="run-test"
              loading={submitting}
              disabled={runDisabled}
              onClick={() => void handleRun()}
            >
              运行测试
            </Button>
          </ActionWithReason>
          {props.dirty && (
            <span className="test-blocked-guidance" role="status">
              <Typography.Text type="warning">
                存在未保存修改，请先使用顶部“保存新版本”。
              </Typography.Text>
              <Button type="link" size="small" data-testid="return-to-edit" onClick={props.onEdit}>
                返回编辑
              </Button>
            </span>
          )}
        </div>
      </section>

      <section className="execution-col" data-testid="execution-col">
        <h3 className="test-run-col-title">Execution</h3>
        {execution === null ? (
          <div className="execution-empty">
            <Alert
              type="info"
              showIcon
              message="尚未运行测试"
              description="输入 JSON 后点击“运行测试”，这里会显示执行状态与实时日志。"
            />
          </div>
        ) : (
          <div className="execution-body">
            {watcher.fallbackExhausted && !isTerminal(execution.status) && (
              <Alert
                type="warning"
                showIcon
                data-testid="fallback-exhausted"
                message="实时连接已断开，状态可能已过期"
                description="已按权威结果轮询至上限仍未等到终态，请刷新页面或稍后重新查看该 Execution。"
              />
            )}
            <div className="execution-summary">
              <span data-testid="execution-id">Execution #{execution.id}</span>
              <Tag color={statusColor(execution.status)} data-testid="execution-status">
                {statusLabel(execution.status)}
              </Tag>
              {execution.error && (
                <Typography.Text type="danger" role="alert" data-testid="execution-error">
                  {execution.error}
                </Typography.Text>
              )}
              <span data-testid="execution-version">
                <span className="execution-summary-label">Version</span>
                {executionVersionSeq !== null ? (
                  <>
                    v{executionVersionSeq}
                    <span className="execution-version-debug">#{execution.version_id}</span>
                  </>
                ) : (
                  `#${execution.version_id}`
                )}
              </span>
              <span
                className="execution-worker-summary"
                data-testid="execution-worker"
                title={
                  execution.worker_id === null
                    ? undefined
                    : (executionWorker?.name ?? `Worker #${execution.worker_id}`)
                }
              >
                <span className="execution-summary-label">Worker</span>
                <span className="execution-worker-name">
                  {execution.worker_id === null
                    ? "—"
                    : (executionWorker?.name ?? `#${execution.worker_id}`)}
                </span>
                {execution.worker_id !== null && executionWorker !== null && (
                  <span className="execution-version-debug">#{execution.worker_id}</span>
                )}
              </span>
              <span>
                <span className="execution-summary-label">耗时</span>
                {formatDuration(execution.duration_ms)}
              </span>
            </div>
            <Tabs
              className="execution-tabs"
              size="small"
              items={[
                {
                  key: "output",
                  label: "Output",
                  children: <OutputView execution={execution} />,
                },
                {
                  key: "stdout",
                  label: "stdout",
                  children: (
                    <LogView
                      testId="stdout-view"
                      content={watcher.liveStdout}
                      truncated={execution.stdout_truncated}
                    />
                  ),
                },
                {
                  key: "stderr",
                  label: "stderr",
                  children: (
                    <LogView
                      testId="stderr-view"
                      content={watcher.liveStderr}
                      truncated={execution.stderr_truncated}
                    />
                  ),
                },
              ]}
            />
          </div>
        )}
      </section>
    </div>
  );
}
