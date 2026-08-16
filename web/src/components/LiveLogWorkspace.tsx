/** Workbench-level live log surface shared by Task, Schedule and Webhook runs. */

import { Alert, Button, Space, Tabs, Tag } from "antd";

import { isTerminal, statusColor, statusLabel } from "../status";
import type { Execution } from "../types";
import { LogView, OutputView } from "./OutputView";

interface Props {
  execution: Execution | null;
  liveStdout: string;
  liveStderr: string;
  fallbackExhausted: boolean;
  waitingForWebhook: boolean;
  open: boolean;
  fullscreen: boolean;
  onOpen: () => void;
  onClose: () => void;
  onEnterFullscreen: () => void;
  onRestoreBottom: () => void;
}

export default function LiveLogWorkspace(props: Props) {
  const hasLiveContext = props.execution !== null || props.waitingForWebhook;
  if (!hasLiveContext) {
    return null;
  }

  if (!props.open) {
    return (
      <div className="live-log-collapsed" data-testid="live-log-collapsed">
        <Button type="text" onClick={props.onOpen}>
          打开实时日志
          {props.execution !== null ? ` · 执行 #${props.execution.id}` : " · 等待 Webhook 请求…"}
        </Button>
      </div>
    );
  }

  const execution = props.execution;
  return (
    <section
      className={`live-log-workspace${props.fullscreen ? " live-log-fullscreen" : ""}`}
      data-testid="live-log-workspace"
      aria-label="实时日志"
    >
      <div className="live-log-header">
        <div className="live-log-title-group">
          <strong>实时日志</strong>
          {execution !== null ? (
            <>
              <span>执行 #{execution.id}</span>
              <Tag color={statusColor(execution.status)}>{statusLabel(execution.status)}</Tag>
            </>
          ) : (
            <Tag color="processing">等待 Webhook 请求…</Tag>
          )}
        </div>
        <Space size="small">
          {props.fullscreen ? (
            <Button size="small" data-testid="live-log-restore" onClick={props.onRestoreBottom}>
              恢复到底部
            </Button>
          ) : (
            <Button size="small" data-testid="live-log-fullscreen" onClick={props.onEnterFullscreen}>
              全屏
            </Button>
          )}
          <Button size="small" type="text" data-testid="live-log-close" onClick={props.onClose}>
            收起
          </Button>
        </Space>
      </div>

      {execution === null ? (
        <div className="live-log-waiting" role="status">
          <span className="live-log-waiting-pulse" aria-hidden="true" />
          <div>
            <strong>等待 Webhook 请求…</strong>
            <p>收到真实请求并创建执行后，这里会自动跟踪 stdout、stderr 与最终结果。</p>
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
            defaultActiveKey="stdout"
            items={[
              {
                key: "stdout",
                label: "stdout",
                children: (
                  <LogView
                    testId="live-log-stdout"
                    content={props.liveStdout}
                    truncated={execution.stdout_truncated}
                  />
                ),
              },
              {
                key: "stderr",
                label: "stderr",
                children: (
                  <LogView
                    testId="live-log-stderr"
                    content={props.liveStderr}
                    truncated={execution.stderr_truncated}
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
