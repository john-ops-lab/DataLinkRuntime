/** Result viewers: formatted Output plus terminal-style stdout/stderr (M3 §8). */

import { useEffect, useRef } from "react";
import { Alert } from "antd";

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
          message="Output 超过平台保存上限，未保存完整内容"
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

/** Terminal-style log pane; follows the tail unless the user scrolled up. */
export function LogView(props: {
  content: string;
  truncated: boolean;
  emptyHint?: string;
  testId?: string;
}) {
  const preRef = useRef<HTMLPreElement | null>(null);
  // The user owns the scroll position once they scroll up; only auto-follow
  // while they stay near the bottom (simple strategy per M3 spec §8.2).
  const followTail = useRef(true);

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
    followTail.current = element.scrollTop + element.clientHeight >= element.scrollHeight - 24;
  }

  return (
    <div className="log-pane">
      {props.truncated && (
        <Alert type="warning" showIcon banner message="日志超过平台保存上限，部分内容已被截断" />
      )}
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
