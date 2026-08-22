/** Result viewers: formatted Output plus the unified terminal log (M3 §8). */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Alert, Button, Input, Tooltip } from "antd";
import {
  CopyOutlined,
  DownloadOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  PauseOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import { useTranslation } from "react-i18next";

import type { Execution } from "../types";
import { tailLogLines } from "../unified-log";

/** Full output as formatted JSON; truncated output shows size + preview. */
export function OutputView(props: { execution: Execution; testId?: string }) {
  const { execution } = props;
  const { t } = useTranslation(["runtime", "common"]);
  if (execution.output_truncated) {
    return (
      <div className="output-view" data-testid={props.testId ?? "output-truncated"}>
        <Alert
          type="warning"
          showIcon
          message={t("output.truncated")}
          description={t("output.truncatedDescription", {
            size: execution.output_size ?? t("output.unknownSize"),
          })}
        />
        <pre className="terminal-view" data-testid="output-preview" aria-label={t("output.contentLabel")}>
          {execution.output_preview ?? ""}
        </pre>
      </div>
    );
  }
  if (execution.output === null || execution.output === undefined) {
    return (
      <div className="output-view output-empty" data-testid={props.testId ?? "output-empty"}>
        {t("output.empty")}
      </div>
    );
  }
  return (
    <pre className="output-view" data-testid={props.testId ?? "output-content"} aria-label={t("output.contentLabel")}>
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

function logicalLineCount(content: string): number {
  if (content === "") {
    return 0;
  }
  return content.endsWith("\n") ? content.split("\n").length - 1 : content.split("\n").length;
}

function filterLogLines(content: string, query: string): { content: string; matches: number } {
  const normalized = query.trim().toLocaleLowerCase();
  if (normalized === "") {
    return { content, matches: 0 };
  }
  const hasTrailingNewline = content.endsWith("\n");
  const lines = content.split("\n");
  if (hasTrailingNewline) {
    lines.pop();
  }
  const matchingLines = lines.filter((line) => line.toLocaleLowerCase().includes(normalized));
  return {
    content: `${matchingLines.join("\n")}${hasTrailingNewline && matchingLines.length > 0 ? "\n" : ""}`,
    matches: matchingLines.length,
  };
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
  /** True when the live pane is only rendering the newest browser window. */
  browserWindowTruncated?: boolean;
  /** Size of the live browser rendering window shown in the notice. */
  browserWindowLines?: number;
  /** Open the full server-saved Execution detail from the live view. */
  onViewServerLog?: () => void;
  /** Stable filename stem for downloading a history snapshot. */
  downloadFileName?: string;
  /** 实时日志选区 → AI 上下文（只使用浏览器可见的已脱敏文本）。 */
  addContextLabel?: string;
  onAddContext?: (text: string, startLine: number, endLine: number) => void;
}) {
  const { t } = useTranslation("runtime");
  const preRef = useRef<HTMLPreElement | null>(null);
  // The scroll offset is part of the user's view state. Keeping it separate
  // from React state lets a live-log append restore the exact paused/history
  // position after the new DOM text has been committed.
  const scrollTopRef = useRef(0);
  const followControls = props.followControls ?? true;
  const mode = props.mode ?? "live";
  const [searchQuery, setSearchQuery] = useState("");
  const [copyState, setCopyState] = useState<"idle" | "success" | "failed">("idle");
  // The user owns the live scroll position once they scroll up or pause; a
  // history view never follows because it is a completed audit snapshot.
  const followTail = useRef(followControls);
  const pausedLineBaselineRef = useRef<number | null>(null);
  const [paused, setPaused] = useState(false);
  const [newLineCount, setNewLineCount] = useState(0);
  const [hasSelection, setHasSelection] = useState(false);
  const [maximized, setMaximized] = useState(false);
  const maximizeButtonRef = useRef<HTMLButtonElement>(null);
  const wasMaximizedRef = useRef(false);
  const searchResult = mode === "history" ? filterLogLines(props.content, searchQuery) : null;
  const filteredContent = searchResult?.content ?? props.content;
  const displayContent =
    props.maxLines === undefined ? filteredContent : tailLogLines(filteredContent, props.maxLines);

  useEffect(() => {
    if (followControls && !followTail.current && pausedLineBaselineRef.current !== null) {
      const currentLines = logicalLineCount(props.content);
      const delta = currentLines - pausedLineBaselineRef.current;
      // A capped live window can stay at 2000 lines while new lines replace
      // old ones; any changed content therefore represents at least one new
      // line for the paused reader.
      setNewLineCount((current) =>
        delta > 0 ? Math.max(current, delta) : (current > 0 ? current + 1 : 1),
      );
    }
  }, [followControls, props.content]);

  useEffect(() => {
    if (!maximized) {
      return;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") {
        return;
      }
      event.preventDefault();
      // A history LogView can live inside an Ant Design Drawer. Stop the
      // Drawer-level Escape handler from closing the entire detail view when
      // the user only intends to restore this log pane.
      event.stopPropagation();
      setMaximized(false);
    };
    // Ant Design Drawer handles Escape during capture; register here too so
    // restoring the pane wins without closing the surrounding detail drawer.
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, [maximized]);

  useEffect(() => {
    if (maximized) {
      maximizeButtonRef.current?.focus();
    } else if (wasMaximizedRef.current) {
      maximizeButtonRef.current?.focus();
    }
    wasMaximizedRef.current = maximized;
  }, [maximized]);

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

  useLayoutEffect(() => {
    const element = preRef.current;
    if (element === null) {
      return;
    }
    if (followTail.current) {
      element.scrollTop = element.scrollHeight;
      scrollTopRef.current = element.scrollTop;
    } else {
      // Appending live output or refreshing a terminal Execution must not
      // reclaim the bottom after the user paused or inspected history.
      element.scrollTop = scrollTopRef.current;
    }
  }, [displayContent]);

  function handleScroll() {
    const element = preRef.current;
    if (element === null) {
      return;
    }
    scrollTopRef.current = element.scrollTop;
    if (!followControls) {
      return;
    }
    const nearBottom = element.scrollTop + element.clientHeight >= element.scrollHeight - 24;
    followTail.current = nearBottom;
    setPaused(!nearBottom);
    if (nearBottom) {
      setNewLineCount(0);
      pausedLineBaselineRef.current = null;
    } else if (pausedLineBaselineRef.current === null) {
      pausedLineBaselineRef.current = logicalLineCount(props.content);
    }
  }

  function resumeFollowing() {
    followTail.current = true;
    setPaused(false);
    setNewLineCount(0);
    pausedLineBaselineRef.current = null;
    const element = preRef.current;
    if (element !== null) {
      element.scrollTop = element.scrollHeight;
      scrollTopRef.current = element.scrollTop;
    }
  }

  async function copySavedContent() {
    try {
      if (navigator.clipboard?.writeText !== undefined) {
        await navigator.clipboard.writeText(props.content);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = props.content;
        textarea.setAttribute("readonly", "true");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        const copied = document.execCommand?.("copy") ?? false;
        textarea.remove();
        if (!copied) {
          throw new Error("clipboard unavailable");
        }
      }
      setCopyState("success");
    } catch {
      setCopyState("failed");
    }
  }

  function downloadSavedContent() {
    const blob = new Blob([props.content], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${props.downloadFileName ?? "execution-log"}.log`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  const paneClassName = [
    "log-pane",
    mode === "history" ? "history-log-pane" : "live-log-pane",
    maximized ? "log-pane-maximized" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={paneClassName}
      data-log-view-mode={mode}
      role="region"
      aria-label={t("logs.title")}
    >
      <div className="log-toolbar">
        {mode === "history" && (
          <div className="log-history-tools" role="group" aria-label={t("logs.historyTools")}>
            <Input
              size="small"
              data-testid={props.testId ? `${props.testId}-search` : "log-search"}
              prefix={<SearchOutlined aria-hidden="true" />}
              aria-label={t("logs.search")}
              placeholder={t("logs.searchPlaceholder")}
              allowClear
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
            />
            {searchResult !== null && searchQuery.trim() !== "" && (
              <span className="log-search-count" aria-live="polite">
                {t("logs.matchCount", { count: searchResult.matches })}
              </span>
            )}
            <Tooltip title={t("logs.copy")} trigger={["hover", "focus"]}>
              <Button
                size="small"
                type="text"
                data-testid={props.testId ? `${props.testId}-copy` : "log-copy"}
                icon={<CopyOutlined aria-hidden="true" />}
                aria-label={t("logs.copy")}
                onClick={() => void copySavedContent()}
              />
            </Tooltip>
            <Tooltip title={t("logs.download")} trigger={["hover", "focus"]}>
              <Button
                size="small"
                type="text"
                data-testid={props.testId ? `${props.testId}-download` : "log-download"}
                icon={<DownloadOutlined aria-hidden="true" />}
                aria-label={t("logs.download")}
                onClick={downloadSavedContent}
              />
            </Tooltip>
          </div>
        )}
        {followControls &&
          (paused ? (
            <Button
              size="small"
              data-testid={props.testId ? `${props.testId}-resume` : "log-resume"}
              icon={<PlayCircleOutlined aria-hidden="true" />}
              aria-label={t("logs.resume")}
              onClick={resumeFollowing}
            >
              {t("logs.resume")}
            </Button>
          ) : (
            <Button
              size="small"
              data-testid={props.testId ? `${props.testId}-pause` : "log-pause"}
              icon={<PauseOutlined aria-hidden="true" />}
              aria-label={t("logs.pause")}
              onClick={() => {
                const element = preRef.current;
                if (element !== null) {
                  scrollTopRef.current = element.scrollTop;
                }
                followTail.current = false;
                pausedLineBaselineRef.current = logicalLineCount(props.content);
                setPaused(true);
              }}
            >
              {t("logs.pause")}
            </Button>
          ))}
        {props.onAddContext !== undefined && (
          <Button
            size="small"
            data-testid={props.testId ? `${props.testId}-add-context` : "log-add-context"}
            disabled={!hasSelection}
            icon={<PlusOutlined aria-hidden="true" />}
            aria-label={props.addContextLabel ?? t("actions.addContext", { ns: "common" })}
            title={t("logs.addContextTitle")}
            onClick={handleAddContext}
          >
            {props.addContextLabel ?? t("actions.addContext", { ns: "common" })}
          </Button>
        )}
        {newLineCount > 0 && (
          <span
            className="log-new-count"
            data-testid={props.testId ? `${props.testId}-new-count` : "log-new-count"}
            role="status"
          >
            {t("logs.newLines", { count: newLineCount })}
          </span>
        )}
        <Tooltip title={maximized ? t("logs.restore") : t("logs.maximize")} trigger={["hover", "focus"]}>
          <Button
            ref={maximizeButtonRef}
            size="small"
            type="text"
            data-testid={
              props.testId
                ? `${props.testId}-${maximized ? "restore" : "maximize"}`
                : `log-${maximized ? "restore" : "maximize"}`
            }
            aria-label={maximized ? t("logs.restore") : t("logs.maximize")}
            aria-pressed={maximized}
            icon={maximized ? <FullscreenExitOutlined aria-hidden="true" /> : <FullscreenOutlined aria-hidden="true" />}
            onClick={() => setMaximized((current) => !current)}
          />
        </Tooltip>
      </div>
      {(props.browserWindowTruncated || props.truncated || copyState !== "idle") && (
        <div className="log-notices">
          {props.browserWindowTruncated && (
            <Alert
              type="info"
              showIcon
              data-testid={props.testId ? `${props.testId}-browser-window` : "log-browser-window"}
              message={t("logs.browserWindow", { count: props.browserWindowLines ?? 2000 })}
              description={
                props.onViewServerLog !== undefined ? (
                  <span>
                    {t("logs.browserWindowDescription")} {" "}
                    <Button
                      type="link"
                      size="small"
                      data-testid={props.testId ? `${props.testId}-view-server` : "log-view-server"}
                      onClick={props.onViewServerLog}
                    >
                      {t("logs.viewServer")}
                    </Button>
                  </span>
                ) : (
                  t("logs.browserWindowDescription")
                )
              }
            />
          )}
          {props.truncated && (
            <Alert
              type="warning"
              showIcon
              data-testid={props.testId ? `${props.testId}-server-truncated` : "log-server-truncated"}
              message={t("logs.serverTruncated")}
              description={t("logs.serverTruncatedDescription")}
            />
          )}
          {copyState !== "idle" && (
            <span className="log-copy-status" role="status">
              {copyState === "success" ? t("logs.copySuccess") : t("logs.copyFailed")}
            </span>
          )}
        </div>
      )}
      <pre
        ref={preRef}
        className="terminal-view"
        data-testid={props.testId ?? "log-view"}
        aria-label={t("logs.contentLabel")}
        onScroll={handleScroll}
      >
        {displayContent === ""
          ? (searchQuery.trim() !== "" ? t("logs.noMatches") : (props.emptyHint ?? t("logs.empty")))
          : displayContent}
      </pre>
    </div>
  );
}
