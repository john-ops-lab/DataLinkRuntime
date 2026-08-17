/**
 * Workbench 实时日志 Tab（M5.5.10）。
 *
 * Task 与 Webhook 共用的独立「实时日志」页面：按实际发生顺序统一展示
 * stdout / stderr / context.logger / Traceback / 平台消息，每行带统一时间
 * 前缀；不再有覆盖编辑页与运行设置的底部浮层，也不显示内部 Execution #N。
 */

import { Alert, Tabs, Tag } from "antd";

import { isTerminal, statusColor, statusLabel } from "../status";
import type { AiContextSnippet, Execution } from "../types";
import { LIVE_LOG_MAX_LINES, unifiedLogContent } from "../unified-log";
import { LogView, OutputView } from "./OutputView";

interface Props {
  execution: Execution | null;
  liveStdout: string;
  liveStderr: string;
  fallbackExhausted: boolean;
  waitingForWebhook: boolean;
  /** M5.5.13: user selected masked browser-visible log text to add to the AI
   * context. Only the already-rendered (masked) text ever leaves this view. */
  onAddContext?: (snippet: AiContextSnippet) => void;
}

export default function LiveLogWorkspace(props: Props) {
  const execution = props.execution;
  const content = unifiedLogContent(props.liveStdout, props.liveStderr);

  function handleAddContext(text: string, startLine: number, endLine: number) {
    if (props.onAddContext === undefined) {
      return;
    }
    props.onAddContext({ source: "log", text, start_line: startLine, end_line: endLine });
  }

  return (
    <section className="live-log-workspace" data-testid="live-log-workspace" aria-label="实时日志">
      <div className="live-log-header">
        <div className="live-log-title-group">
          <strong>实时日志</strong>
          {execution !== null ? (
            <Tag color={statusColor(execution.status)}>{statusLabel(execution.status)}</Tag>
          ) : props.waitingForWebhook ? (
            <Tag color="processing">等待 Webhook 请求…</Tag>
          ) : (
            <Tag>暂无日志</Tag>
          )}
        </div>
      </div>

      {execution === null && props.waitingForWebhook ? (
        <div className="live-log-waiting" role="status">
          <span className="live-log-waiting-pulse" aria-hidden="true" />
          <div>
            <strong>等待 Webhook 请求…</strong>
            <p>收到真实请求并创建执行后，这里会自动跟踪本次调用的完整日志。</p>
          </div>
        </div>
      ) : execution === null ? (
        <div className="live-log-waiting" role="status">
          <div>
            <strong>暂无实时日志</strong>
            <p>运行开始后，stdout、stderr、logger 与 Traceback 会按实际顺序显示在这里。</p>
          </div>
        </div>
      ) : (
        <div className="live-log-content">
          {props.fallbackExhausted && !isTerminal(execution.status) && (
            <Alert
              type="warning"
              showIcon
              message="实时连接已断开，状态可能已过期"
              description="已按权威结果轮询至上限仍未等到终态，请刷新或稍后到执行记录中查看。"
            />
          )}
          <Tabs
            className="live-log-tabs"
            defaultActiveKey="log"
            items={[
              {
                key: "log",
                label: "统一日志",
                children: (
                  <LogView
                    testId="live-log"
                    content={content}
                    truncated={execution.stdout_truncated || execution.stderr_truncated}
                    emptyHint="暂无日志"
                    maxLines={LIVE_LOG_MAX_LINES}
                    mode="live"
                    addContextLabel="加入对话上下文"
                    onAddContext={handleAddContext}
                  />
                ),
              },
              {
                key: "output",
                label: "输出",
                children: <OutputView execution={execution} />,
              },
            ]}
          />
        </div>
      )}
    </section>
  );
}
