/**
 * Workbench 实时日志 Tab（M5.5.10）。
 *
 * Task 与 Webhook 共用的独立「实时日志」页面：按实际发生顺序统一展示
 * stdout / stderr / context.logger / Traceback / 平台消息，每行带统一时间
 * 前缀；不再有覆盖编辑页与运行设置的底部浮层，也不显示内部 Execution #N。
 */

import { Alert, Tabs, Tag } from "antd";
import { useTranslation } from "react-i18next";

import { isTerminal, statusColor, statusLabel } from "../status";
import type { AiContextSnippet, Execution } from "../types";
import { LIVE_LOG_MAX_LINES, logLineCount, unifiedLogContent } from "../unified-log";
import { LogView, OutputView } from "./OutputView";

interface Props {
  execution: Execution | null;
  liveStdout: string;
  liveStderr: string;
  /** Logical count of the server-saved streams; unlike liveStdout this is not capped. */
  serverLogLineCount?: number;
  fallbackExhausted: boolean;
  waitingForWebhook: boolean;
  /** M5.5.13: user selected masked browser-visible log text to add to the AI
   * context. Only the already-rendered (masked) text ever leaves this view. */
  onAddContext?: (snippet: AiContextSnippet) => void;
  /** Switch to the history drawer for the server-saved Execution content. */
  onViewServerLog?: () => void;
}

export default function LiveLogWorkspace(props: Props) {
  const { t } = useTranslation("runtime");
  const execution = props.execution;
  const content = unifiedLogContent(props.liveStdout, props.liveStderr);
  const serverContent = execution === null
    ? ""
    : unifiedLogContent(execution.stdout, execution.stderr, execution.error);
  const serverLineCount = props.serverLogLineCount ?? logLineCount(serverContent);
  const browserWindowTruncated = serverLineCount > LIVE_LOG_MAX_LINES;

  function handleAddContext(text: string, startLine: number, endLine: number) {
    if (props.onAddContext === undefined) {
      return;
    }
    props.onAddContext({ source: "log", text, start_line: startLine, end_line: endLine });
  }

  return (
    <section className="live-log-workspace" data-testid="live-log-workspace" aria-label={t("live.title")}>
      <div className="live-log-header">
        <div className="live-log-title-group">
            <strong>{t("live.title")}</strong>
          {execution !== null ? (
            <Tag color={statusColor(execution.status)}>{statusLabel(execution.status)}</Tag>
          ) : props.waitingForWebhook ? (
            <Tag color="processing">{t("live.waitingTag")}</Tag>
          ) : (
            <Tag>{t("live.emptyTag")}</Tag>
          )}
        </div>
      </div>

      {execution === null && props.waitingForWebhook ? (
        <div className="live-log-waiting" role="status">
          <span className="live-log-waiting-pulse" aria-hidden="true" />
          <div>
            <strong>{t("live.waitingTitle")}</strong>
            <p>{t("live.waitingDescription")}</p>
          </div>
        </div>
      ) : execution === null ? (
        <div className="live-log-waiting" role="status">
          <div>
            <strong>{t("live.emptyTitle")}</strong>
            <p>{t("live.emptyDescription")}</p>
          </div>
        </div>
      ) : (
        <div className="live-log-content">
          {props.fallbackExhausted && !isTerminal(execution.status) && (
            <Alert
              type="warning"
              showIcon
              message={t("live.connectionLost")}
              description={t("live.connectionLostDescription")}
            />
          )}
          <Tabs
            className="live-log-tabs"
            defaultActiveKey="log"
            items={[
              {
                key: "log",
                label: t("live.unifiedLog"),
                children: (
                  <LogView
                    key={execution.id}
                    testId="live-log"
                    content={content}
                    truncated={execution.stdout_truncated || execution.stderr_truncated}
                    emptyHint={t("logs.empty")}
                    maxLines={LIVE_LOG_MAX_LINES}
                    mode="live"
                    browserWindowTruncated={browserWindowTruncated}
                    browserWindowLines={LIVE_LOG_MAX_LINES}
                    onViewServerLog={props.onViewServerLog}
                    addContextLabel={t("actions.addContext", { ns: "common" })}
                    onAddContext={handleAddContext}
                  />
                ),
              },
              {
                key: "output",
                label: t("live.output"),
                children: <OutputView execution={execution} />,
              },
            ]}
          />
        </div>
      )}
    </section>
  );
}
