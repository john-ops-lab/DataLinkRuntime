/** Result viewers: formatted Output plus the unified terminal log (M3 §8). */

import { useEffect, useRef, useState } from "react";
import { Alert, Button } from "antd";

import type { Execution } from "../types";
import { tailLogLines } from "../unified-log";

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

/** M5.5.13: 1-based line number of a selection boundary inside an element's
 * visible text (works with text-node ranges). */
function lineNumberAtOffset(
  element: HTMLElement,
  range: Range,
  boundary: "start" | "end",
): number {
  const probe = document.createRange();
  probe.selectNodeContents(element);
  if (boundary === "start") {
    probe.setEnd(range.startContainer, range.startOffset);
  } else {
    probe.setEnd(range.endContainer, range.endOffset);
  }
  return probe.toString().split("\n").length;
}

/**
 * Terminal-style unified log pane (M5.5.10).
 *
 * Scroll contract:
 * - follows the newest lines by default;
 * - clicking 暂停 or scrolling up stays at the current position and new
 *   content never yanks the view back to the bottom;
 * - clicking 继续跟随 resumes following the tail.
 *
 * M5.5.13: when onAddContext is provided, the toolbar offers 加入对话上下文
 * which reads ONLY the current browser selection inside the pre (the already
 * masked, browser-visible text). Raw logs are never read here.
 */
export function LogView(props: {
  content: string;
  truncated: boolean;
  emptyHint?: string;
  testId?: string;
  maxLines?: number;
  mode?: "live" | "history";
  followControls?: boolean;
  /** 实时日志选区 → AI 上下文（只使用浏览器可见的已脱敏文本）。 */
  addContextLabel?: string;
  onAddContext?: (text: string, startLine: number, endLine: number) => void;
}) {
  const preRef = useRef<HTMLPreElement | null>(null);
  const followControls = props.followControls ?? true;
  const mode = props.mode ?? "live";
  // The user owns the live scroll position once they scroll up or pause; a
  // history view never follows because it is a completed audit snapshot.
  const followTail = useRef(followControls);
  const [paused, setPaused] = useState(false);
  const [hasSelection, setHasSelection] = useState(false);
  const [maximized, setMaximized] = useState(false);
  const displayContent =
    props.maxLines === undefined ? props.content : tailLogLines(props.content, props.maxLines);

  function rangeIsInside(element: HTMLElement, range: Range): boolean {
    return element.contains(range.startContainer) && element.contains(range.endContainer);
  }

  // Track whether the current document selection lives inside this log pane,
  // so 加入对话上下文 is only offered for real in-pane selections.
  useEffect(() => {
    function updateSelectionState() {
      const element = preRef.current;
      const selection = window.getSelection();
      if (element === null || selection === null || selection.isCollapsed) {
        setHasSelection(false);
        return;
      }
      if (selection.rangeCount === 0) {
        setHasSelection(false);
        return;
      }
      const range = selection.getRangeAt(0);
      const anchor = selection.anchorNode;
      const focus = selection.focusNode;
      setHasSelection(
        anchor !== null &&
          focus !== null &&
          element.contains(anchor) &&
          element.contains(focus) &&
          rangeIsInside(element, range) &&
          selection.toString().trim() !== "",
      );
    }
    document.addEventListener("selectionchange", updateSelectionState);
    updateSelectionState();
    return () => {
      document.removeEventListener("selectionchange", updateSelectionState);
    };
  }, [displayContent]);

  function handleAddContext() {
    const element = preRef.current;
    const selection = window.getSelection();
    if (
      element === null ||
      selection === null ||
      selection.isCollapsed ||
      selection.rangeCount === 0
    ) {
      return;
    }
    const range = selection.getRangeAt(0);
    if (!rangeIsInside(element, range)) {
      return;
    }
    const text = selection.toString();
    if (text.trim() === "") {
      return;
    }
    // 1-based line range of the selection inside the pane's visible text.
    const startLine = lineNumberAtOffset(element, range, "start");
    const endLine = lineNumberAtOffset(element, range, "end");
    props.onAddContext?.(text, startLine, endLine);
  }

  useEffect(() => {
    const element = preRef.current;
    if (element !== null && followTail.current) {
      element.scrollTop = element.scrollHeight;
    }
  }, [displayContent]);

  function handleScroll() {
    if (!followControls) {
      return;
    }
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

  const paneClassName = [
    "log-pane",
    mode === "history" ? "history-log-pane" : "live-log-pane",
    maximized ? "log-pane-maximized" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={paneClassName} data-log-view-mode={mode}>
      <div className="log-toolbar">
        {followControls &&
          (paused ? (
            <Button
              size="small"
              data-testid={props.testId ? `${props.testId}-resume` : "log-resume"}
              onClick={resumeFollowing}
            >
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
          ))}
        {props.onAddContext !== undefined && (
          <Button
            size="small"
            data-testid={props.testId ? `${props.testId}-add-context` : "log-add-context"}
            disabled={!hasSelection}
            title="把当前选中的日志文本加入 AI 对话上下文（仅使用浏览器可见的已脱敏文本）"
            onClick={handleAddContext}
          >
            {props.addContextLabel ?? "加入对话上下文"}
          </Button>
        )}
        <Button
          size="small"
          data-testid={
            props.testId
              ? `${props.testId}-${maximized ? "restore" : "maximize"}`
              : `log-${maximized ? "restore" : "maximize"}`
          }
          aria-label={maximized ? "恢复日志大小" : "最大化日志"}
          onClick={() => setMaximized((current) => !current)}
        >
          {maximized ? "恢复" : "最大化"}
        </Button>
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
        {displayContent === "" ? (props.emptyHint ?? "暂无日志") : displayContent}
      </pre>
    </div>
  );
}
