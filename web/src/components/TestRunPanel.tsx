/** 测试运行 Tab：Input JSON + 显式绑定选中 Version + 实时日志（M3 §4/§7/§8）。 */

import { useEffect, useRef, useState } from "react";
import { Alert, Button, Descriptions, Space, Tabs, Tag, Typography } from "antd";

import { ApiError, api } from "../api";
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

// After an unexpected SSE end, converge on the authoritative M2 result with one
// immediate GET plus bounded polling (no reconnect framework needed).
const FALLBACK_POLL_INTERVAL_MS = 1000;
const FALLBACK_MAX_POLLS = 10;

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
  const streamRef = useRef<ExecutionEventsHandle | null>(null);
  const fallbackTimerRef = useRef<number | null>(null);

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

  // SSE is only the experience channel: when it dies before a terminal
  // event, the M2 final result stays authoritative, so converge on it.
  function convergeOnFinalResult(executionId: number, remainingPolls: number) {
    api
      .getExecution(executionId)
      .then((detail) => {
        setExecution(detail);
        setLiveStdout(detail.stdout);
        setLiveStderr(detail.stderr);
        if (isTerminal(detail.status) || remainingPolls <= 0) {
          return;
        }
        fallbackTimerRef.current = window.setTimeout(
          () => convergeOnFinalResult(executionId, remainingPolls - 1),
          FALLBACK_POLL_INTERVAL_MS,
        );
      })
      .catch((error) => props.onError(errorMessage(error)));
  }

  function watch(executionId: number) {
    streamRef.current?.close();
    stopFallbackPolling();
    streamRef.current = openExecutionEvents(executionId, {
      onExecution(next) {
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
            .then((detail) => {
              setExecution(detail);
              setLiveStdout(detail.stdout);
              setLiveStderr(detail.stderr);
            })
            .catch((error) => props.onError(errorMessage(error)));
        }
      },
      onLog(event) {
        if (event.stream === "stdout") {
          setLiveStdout((current) => current + event.chunk);
        } else {
          setLiveStderr((current) => current + event.chunk);
        }
      },
      onLogSnapshot(event) {
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
        streamRef.current?.close();
        convergeOnFinalResult(executionId, FALLBACK_MAX_POLLS);
      },
      onError(message) {
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
      setExecution(created);
      setLiveStdout(created.stdout);
      setLiveStderr(created.stderr);
      watch(created.id);
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
