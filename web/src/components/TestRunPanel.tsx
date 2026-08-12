/** 测试运行 Tab：Input JSON + 显式绑定选中 Version + 实时日志（M3 §4/§7/§8）。 */

import { useState } from "react";
import { Alert, Button, Tabs, Tag, Typography } from "antd";

import { ApiError, api } from "../api";
import { useExecutionWatcher } from "../hooks/useExecutionWatcher";
import { isTerminal, statusColor, statusLabel } from "../status";
import type { Adapter } from "../types";
import { LogView, OutputView } from "./OutputView";

interface TestRunPanelProps {
  adapter: Adapter;
  selectedVersionId: number | null;
  selectedVersionSeq: number | null;
  isLatest: boolean;
  isPublished: boolean;
  dirty: boolean;
  contentReady: boolean;
  busy: boolean;
  onError: (message: string) => void;
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
  const [inputText, setInputText] = useState("{}");
  const [submitting, setSubmitting] = useState(false);
  const watcher = useExecutionWatcher(props.onError);
  const execution = watcher.execution;
  // version_id -> seq resolved locally at run time: the summary must keep
  // showing the version that was actually executed even after the user
  // switches to another version (Review round 1 Minor 1). No extra request;
  // the internal version_id stays as secondary debug info only. Kept as
  // state (not a ref) because it is read during render.
  const [runSeqByVersionId, setRunSeqByVersionId] = useState<Map<number, number>>(new Map());

  async function handleRun() {
    // Interaction guards: explicit version binding, no dirty runs, valid
    // JSON input and no duplicate submissions (M3 spec §2.1/§4.2/§4.3).
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
    if (props.selectedVersionId === null || !props.contentReady) {
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
    setSubmitting(true);
    try {
      if (props.selectedVersionSeq !== null) {
        const seq = props.selectedVersionSeq;
        const versionId = props.selectedVersionId;
        setRunSeqByVersionId((current) => new Map(current).set(versionId, seq));
      }
      const created = await api.createExecution(props.adapter.id, {
        input,
        version_id: props.selectedVersionId,
      });
      watcher.watch(created);
    } catch (error) {
      props.onError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  const runDisabled =
    submitting ||
    props.busy ||
    props.dirty ||
    !props.contentReady ||
    !!props.adapter.archived_at ||
    props.selectedVersionId === null;

  // User-facing primary version label is vN; fall back to the internal id
  // only when the seq is genuinely unknown.
  const executionVersionSeq =
    execution === null ? null : (runSeqByVersionId.get(execution.version_id) ?? null);

  return (
    // M3.1 双栏工作台：左栏测试输入，右栏 Execution 状态与实时日志。
    <div className="test-run-panel">
      <section className="test-input-col" data-testid="test-input-col">
        <h3 className="test-run-col-title">测试输入</h3>
        <div className="test-version-row" data-testid="test-run-version">
          <span className="execution-summary-label">测试版本</span>
          <span>{props.selectedVersionSeq !== null ? `v${props.selectedVersionSeq}` : "—"}</span>
          {props.isLatest && <Tag color="blue">Latest</Tag>}
          {props.isPublished && <Tag color="green">Published</Tag>}
        </div>
        <Typography.Text type="secondary">运行将显式绑定上方选中的已保存版本</Typography.Text>
        <div className="test-input-block">
          <textarea
            data-testid="test-input"
            className="code-textarea"
            placeholder="Input（任意合法 JSON）"
            value={inputText}
            disabled={submitting}
            onChange={(event) => setInputText(event.target.value)}
          />
        </div>
        <div className="test-run-actions">
          <Button
            type="primary"
            data-testid="run-test"
            loading={submitting}
            disabled={runDisabled}
            onClick={() => void handleRun()}
          >
            运行测试
          </Button>
          {props.dirty && (
            <Typography.Text type="warning">存在未保存修改，请先保存为新版本</Typography.Text>
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
                <Typography.Text type="danger" data-testid="execution-error">
                  {execution.error}
                </Typography.Text>
              )}
              <span data-testid="execution-version">
                <span className="execution-summary-label">Version</span>
                {executionVersionSeq !== null ? `v${executionVersionSeq}` : `#${execution.version_id}`}
                <span className="execution-version-debug">#{execution.version_id}</span>
              </span>
              <span>
                <span className="execution-summary-label">Worker</span>
                {execution.worker_id ?? "—"}
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
