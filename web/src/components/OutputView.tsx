/** Result viewers: formatted Output plus the unified terminal log (M3 §8). */

import { useEffect, useRef, useState } from "react";
import { Alert, Button } from "antd";

import type { Execution } from "../types";

/** Full output as formatted JSON; truncated output shows size + preview. */
export function OutputView(props: { execution: Execution; testId?: string }) {
  const { execution } = props;
  if (execution.output_truncated) {
    return (
      <div className="output-view" data-testid={props.testId ?? "output-truncated"}>
        <Alert
          type="warning"
          showIcon
          message="输出超过平台保存上限，未保存完整内容"
          description={`实际大小：${execution.output_size ?? "未知"} 字节；以下为内容预览（非完整 JSON）`}
        />
        <pre className="terminal-view" data-testid="output-preview">
          {execution.output_preview ?? ""}
        </pre>
      </div>
    );
  }
  if (execution.output === null || execution.output === undefined) {
    return (
      <div className="output-view output-empty" data-testid={props.testId ?? "output-empty"}>
        无 Output
      </div>
    );
  }
  return (
    <pre className="output-view" data-testid={props.testId ?? "output-content"}>
      {JSON.stringify(execution.output, null, 2)}
    </pre>
  );
}

/** M5.5.10 unified log helpers live in ../unified-log (shared with the
 * Workbench live-log Tab and the execution history detail). */

/**
 * Terminal-style unified log pane (M5.5.10).
 *
 * Scroll contract:
 * - follows the newest lines by default;
 * - clicking 暂停 or scrolling up stays at the current position and new
 *   content never yanks the view back to the bottom;
 * - clicking 继续跟随 resumes following the tail.
 */
export function LogView(props: {
  content: string;
  truncated: boolean;
  emptyHint?: string;
  testId?: string;
}) {
  const preRef = useRef<HTMLPreElement | null>(null);
  // The user owns the scroll position once they scroll up or pause; only
  // auto-follow while they stay near the bottom (M5.5.10 §三).
  const followTail = useRef(true);
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    const element = preRef.current;
    if (element !== null && followTail.current) {
      element.scrollTop = element.scrollHeight;
    }
  }, [props.content]);

  function handleScroll() {
    const element = preRef.current;
    if (element === null) {
      return;
    }
    const nearBottom = element.scrollTop + element.clientHeight >= element.scrollHeight - 24;
    followTail.current = nearBottom;
    setPaused(!nearBottom);
  }

  function resumeFollowing() {
    followTail.current = true;
    setPaused(false);
    const element = preRef.current;
    if (element !== null) {
      element.scrollTop = element.scrollHeight;
    }
  }

  return (
    <div className="log-pane">
      <div className="log-toolbar">
        {paused ? (
          <Button size="small" data-testid={props.testId ? `${props.testId}-resume` : "log-resume"} onClick={resumeFollowing}>
            继续跟随
          </Button>
        ) : (
          <Button
            size="small"
            data-testid={props.testId ? `${props.testId}-pause` : "log-pause"}
            onClick={() => {
              followTail.current = false;
              setPaused(true);
            }}
          >
            暂停跟随
          </Button>
        )}
        {props.truncated && (
          <Alert type="warning" showIcon banner message="日志超过平台保存上限，部分内容已被截断" />
        )}
      </div>
      <pre
        ref={preRef}
        className="terminal-view"
        data-testid={props.testId ?? "log-view"}
        onScroll={handleScroll}
      >
        {props.content === "" ? (props.emptyHint ?? "暂无日志") : props.content}
      </pre>
    </div>
  );
}
