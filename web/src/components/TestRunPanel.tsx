/** 测试运行 Tab：Input JSON + 显式绑定选中 Version + 实时日志（M3 §4/§7/§8）。 */

import { useEffect, useRef, useState } from "react";
import { Alert, Button, Descriptions, Space, Tabs, Tag, Typography } from "antd";

import { ApiError, api } from "../api";
import { FALLBACK_POLICY } from "../fallback-policy";
import { isTerminal, statusColor, statusLabel } from "../status";
import { openExecutionEvents } from "../sse";
import type { ExecutionEventsHandle } from "../sse";
import type { Adapter, Execution } from "../types";
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
// bounded GET polling; the policy lives in fallback-policy.ts (testable).

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "请求失败";
}

export default function TestRunPanel(props: TestRunPanelProps) {
  const [inputText, setInputText] = useState("{}");
  const [submitting, setSubmitting] = useState(false);
  const [execution, setExecution] = useState<Execution | null>(null);
  const [liveStdout, setLiveStdout] = useState("");
  const [liveStderr, setLiveStderr] = useState("");
  const [fallbackExhausted, setFallbackExhausted] = useState(false);
  const streamRef = useRef<ExecutionEventsHandle | null>(null);
  const fallbackTimerRef = useRef<number | null>(null);
  // Every run bumps the generation: only the newest run's async responses
  // (SSE events, terminal detail GET, fallback polls) may commit UI state,
  // so a slow response from an older Execution can never overwrite the
  // Execution the user just started.
  const runGenerationRef = useRef(0);

  function stopFallbackPolling() {
    if (fallbackTimerRef.current !== null) {
      window.clearTimeout(fallbackTimerRef.current);
      fallbackTimerRef.current = null;
    }
  }

  // Close the stream and pending fallback polls on unmount (adapter switch).
  useEffect(
    () => () => {
      streamRef.current?.close();
      stopFallbackPolling();
    },
    [],
  );

  function applyDetail(generation: number, detail: Execution) {
    if (generation !== runGenerationRef.current) {
      return; // a newer run owns the panel now
    }
    setExecution(detail);
    setLiveStdout(detail.stdout);
    setLiveStderr(detail.stderr);
  }

  // SSE is only the experience channel: after any abnormal end the M2 final
  // result stays authoritative, so converge on it with bounded polling.
  // Transient GET failures keep retrying inside the same budget.
  function convergeOnFinalResult(generation: number, executionId: number, remainingPolls: number) {
    api
      .getExecution(executionId)
      .then((detail) => {
        applyDetail(generation, detail);
        if (generation !== runGenerationRef.current) {
          return;
        }
        if (isTerminal(detail.status)) {
          return;
        }
        if (remainingPolls <= 0) {
          // Never silently keep a seemingly live running state: tell the
          // user the realtime channel is gone and the status may be stale.
          setFallbackExhausted(true);
          return;
        }
        fallbackTimerRef.current = window.setTimeout(
          () => convergeOnFinalResult(generation, executionId, remainingPolls - 1),
          FALLBACK_POLICY.pollIntervalMs,
        );
      })
      .catch(() => {
        // Transient GET failure: keep polling inside the bounded budget.
        if (generation !== runGenerationRef.current) {
          return;
        }
        if (remainingPolls <= 0) {
          setFallbackExhausted(true);
          return;
        }
        fallbackTimerRef.current = window.setTimeout(
          () => convergeOnFinalResult(generation, executionId, remainingPolls - 1),
          FALLBACK_POLICY.pollIntervalMs,
        );
      });
  }

  function watch(generation: number, executionId: number) {
    streamRef.current?.close();
    stopFallbackPolling();
    streamRef.current = openExecutionEvents(executionId, {
      onExecution(next) {
        if (generation !== runGenerationRef.current) {
          return; // a newer run owns the panel now
        }
        // Every execution event carries the stored streams at poll time; the
        // log events between polls are deltas on top of it, so replacing the
        // buffers here keeps the live view consistent without duplicates.
        setExecution(next);
        setLiveStdout(next.stdout);
        setLiveStderr(next.stderr);
        if (isTerminal(next.status)) {
          streamRef.current?.close();
          // M2 final result is authoritative: reload the full detail.
          api
            .getExecution(executionId)
            .then((detail) => applyDetail(generation, detail))
            .catch((error) => {
              if (generation !== runGenerationRef.current) {
                return;
              }
              props.onError(errorMessage(error));
            });
        }
      },
      onLog(event) {
        if (generation !== runGenerationRef.current) {
          return;
        }
        if (event.stream === "stdout") {
          setLiveStdout((current) => current + event.chunk);
        } else {
          setLiveStderr((current) => current + event.chunk);
        }
      },
      onLogSnapshot(event) {
        if (generation !== runGenerationRef.current) {
          return;
        }
        if (event.stream === "stdout") {
          setLiveStdout(event.content);
        } else {
          setLiveStderr(event.content);
        }
        // Truncation just happened server-side: reflect it immediately
        // instead of waiting for the next execution event or terminal.
        setExecution((current) =>
          current === null
            ? current
            : event.stream === "stdout"
              ? { ...current, stdout_truncated: event.truncated }
              : { ...current, stderr_truncated: event.truncated },
        );
      },
      onUnexpectedClose() {
        if (generation !== runGenerationRef.current) {
          return;
        }
        streamRef.current?.close();
        convergeOnFinalResult(generation, executionId, FALLBACK_POLICY.maxPolls);
      },
      onError(message) {
        if (generation !== runGenerationRef.current) {
          return;
        }
        props.onError(message);
      },
    });
  }

  async function handleRun() {
    // Interaction guards: explicit version binding, no dirty runs, valid
    // JSON input and no duplicate submissions (M3 spec §2.1/§4.2/§4.3).
    if (submitting || props.busy) {
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
      const created = await api.createExecution(props.adapter.id, {
        input,
        version_id: props.selectedVersionId,
      });
      runGenerationRef.current += 1; // invalidate the previous run's async work
      setFallbackExhausted(false);
      setExecution(created);
      setLiveStdout(created.stdout);
      setLiveStderr(created.stderr);
      watch(runGenerationRef.current, created.id);
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
    props.selectedVersionId === null;

  return (
    <div className="test-run-panel">
      <Descriptions
        size="small"
        column={2}
        items={[
          {
            key: "version",
            label: "测试版本",
            children: (
              <Space size={4} data-testid="test-run-version">
                <span>{props.selectedVersionSeq !== null ? `v${props.selectedVersionSeq}` : "—"}</span>
                {props.isLatest && <Tag color="blue">Latest</Tag>}
                {props.isPublished && <Tag color="green">Published</Tag>}
              </Space>
            ),
          },
          {
            key: "hint",
            label: "绑定说明",
            children: <Typography.Text type="secondary">运行将显式绑定上方选中的已保存版本</Typography.Text>,
          },
        ]}
      />

      <div className="test-input-block">
        <Typography.Text type="secondary">Input（任意合法 JSON）</Typography.Text>
        <textarea
          data-testid="test-input"
          className="code-textarea"
          rows={6}
          value={inputText}
          disabled={submitting}
          onChange={(event) => setInputText(event.target.value)}
        />
      </div>

      <Space>
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
      </Space>

      {execution === null ? (
        <Alert type="info" showIcon message="尚未运行测试" description="输入 JSON 后点击“运行测试”，这里会显示执行状态与实时日志。" />
      ) : (
        <div className="execution-panel">
          {fallbackExhausted && execution !== null && !isTerminal(execution.status) && (
            <Alert
              type="warning"
              showIcon
              data-testid="fallback-exhausted"
              message="实时连接已断开，状态可能已过期"
              description="已按权威结果轮询至上限仍未等到终态，请刷新页面或稍后重新查看该 Execution。"
            />
          )}
          <Space size={12} align="center" className="execution-overview" wrap>
            <span data-testid="execution-id">Execution #{execution.id}</span>
            <Tag color={statusColor(execution.status)} data-testid="execution-status">
              {statusLabel(execution.status)}
            </Tag>
            {execution.error && (
              <Typography.Text type="danger" data-testid="execution-error">
                {execution.error}
              </Typography.Text>
            )}
          </Space>
          <Tabs
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
                    content={liveStdout}
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
                    content={liveStderr}
                    truncated={execution.stderr_truncated}
                  />
                ),
              },
            ]}
          />
        </div>
      )}
    </div>
  );
}
