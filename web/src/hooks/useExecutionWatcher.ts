/** 共享的 Execution 实时监视 hook（M3.2）。
 *
 * 从 TestRunPanel 抽取：SSE 只是体验通道，任何异常结束后都收敛到权威的
 * Execution API（有界 GET 轮询，策略见 fallback-policy.ts）。测试运行面板与
 * 执行记录详情抽屉共用同一套实时日志 + 收敛行为，不引入状态框架。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "../api";
import { FALLBACK_POLICY } from "../fallback-policy";
import { openExecutionEvents } from "../sse";
import type { ExecutionEventsHandle } from "../sse";
import { isTerminal } from "../status";
import type { Execution } from "../types";
import { LIVE_LOG_MAX_LINES, logLineCount, tailLogLines, unifiedLogContent } from "../unified-log";
import { userErrorMessage } from "../user-message";

export interface ExecutionWatcher {
  execution: Execution | null;
  liveStdout: string;
  liveStderr: string;
  /** Logical line count of the server-saved streams, not the 2000-line view. */
  serverLogLineCount: number;
  /** True when the bounded fallback polling ended without a terminal status. */
  fallbackExhausted: boolean;
  /** Start (or restart) watching one Execution; resets the live buffers. */
  watch: (initial: Execution) => void;
  /** Invalidate all in-flight events/polls and close the stream. */
  stop: () => void;
}

function errorMessage(error: unknown): string {
  return userErrorMessage(error);
}

function retainLiveLines(content: string): string {
  return tailLogLines(content, LIVE_LOG_MAX_LINES);
}

export function useExecutionWatcher(onError: (message: string) => void): ExecutionWatcher {
  const [execution, setExecution] = useState<Execution | null>(null);
  const [liveStdout, setLiveStdout] = useState("");
  const [liveStderr, setLiveStderr] = useState("");
  const [serverLogLineCount, setServerLogLineCount] = useState(0);
  const serverStdoutRef = useRef("");
  const serverStderrRef = useRef("");
  const [fallbackExhausted, setFallbackExhausted] = useState(false);
  const streamRef = useRef<ExecutionEventsHandle | null>(null);
  const fallbackTimerRef = useRef<number | null>(null);
  // Every watch() bumps the generation: only the newest watch's async work
  // (SSE events, terminal detail GET, fallback polls) may commit UI state, so
  // a slow response from an older Execution can never overwrite the newest.
  const generationRef = useRef(0);
  // Callers usually pass a state setter or inline closure: keep the newest
  // callback without resubscribing the stream (assignment in an effect, not
  // during render, so the lint rule stays satisfied).
  const onErrorRef = useRef(onError);
  useEffect(() => {
    onErrorRef.current = onError;
  });

  function stopFallbackPolling() {
    if (fallbackTimerRef.current !== null) {
      window.clearTimeout(fallbackTimerRef.current);
      fallbackTimerRef.current = null;
    }
  }

  const stop = useCallback(() => {
    generationRef.current += 1; // invalidate in-flight events and polls
    streamRef.current?.close();
    streamRef.current = null;
    stopFallbackPolling();
  }, []);

  // Close the stream and pending fallback polls on unmount (adapter switch).
  useEffect(() => stop, [stop]);

  function applyDetail(generation: number, detail: Execution) {
    if (generation !== generationRef.current) {
      return; // a newer watch owns the view now
    }
    setExecution(detail);
    setLiveStdout(retainLiveLines(detail.stdout));
    setLiveStderr(retainLiveLines(detail.stderr));
    serverStdoutRef.current = detail.stdout;
    serverStderrRef.current = detail.stderr;
    setServerLogLineCount(
      logLineCount(unifiedLogContent(detail.stdout, detail.stderr, detail.error)),
    );
  }

  function replaceSavedStream(stream: "stdout" | "stderr", content: string): void {
    if (stream === "stdout") {
      serverStdoutRef.current = content;
    } else {
      serverStderrRef.current = content;
    }
    setServerLogLineCount(
      logLineCount(unifiedLogContent(serverStdoutRef.current, serverStderrRef.current)),
    );
  }

  function appendSavedStream(stream: "stdout" | "stderr", chunk: string): void {
    if (stream === "stdout") {
      serverStdoutRef.current += chunk;
    } else {
      serverStderrRef.current += chunk;
    }
    setServerLogLineCount(
      logLineCount(unifiedLogContent(serverStdoutRef.current, serverStderrRef.current)),
    );
  }

  // SSE is only the experience channel: after any abnormal end the final
  // result stays authoritative, so converge on it with bounded polling.
  // Transient GET failures keep retrying inside the same budget.
  function convergeOnFinalResult(generation: number, executionId: number, remainingPolls: number) {
    api
      .getExecution(executionId)
      .then((detail) => {
        applyDetail(generation, detail);
        if (generation !== generationRef.current) {
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
        if (generation !== generationRef.current) {
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

  function watch(initial: Execution) {
    generationRef.current += 1; // invalidate the previous watch's async work
    const generation = generationRef.current;
    setFallbackExhausted(false);
    setExecution(initial);
    setLiveStdout(retainLiveLines(initial.stdout));
    setLiveStderr(retainLiveLines(initial.stderr));
    serverStdoutRef.current = initial.stdout;
    serverStderrRef.current = initial.stderr;
    setServerLogLineCount(
      logLineCount(unifiedLogContent(initial.stdout, initial.stderr, initial.error)),
    );
    streamRef.current?.close();
    stopFallbackPolling();
    if (isTerminal(initial.status)) {
      return; // nothing live to follow; the detail is already authoritative
    }
    streamRef.current = openExecutionEvents(initial.id, {
      onExecution(next) {
        if (generation !== generationRef.current) {
          return; // a newer watch owns the view now
        }
        // Every execution event carries the stored streams at poll time; the
        // log events between polls are deltas on top of it, so replacing the
        // buffers here keeps the live view consistent without duplicates.
        setExecution(next);
        // Status events carry the authoritative stored streams. Ignore an
        // older, shorter snapshot that could arrive after a newer log delta;
        // log_snapshot events below replace the refs when server truncation
        // changes the stored prefix.
        if (isTerminal(next.status) || next.stdout.length >= serverStdoutRef.current.length) {
          replaceSavedStream("stdout", next.stdout);
        }
        if (isTerminal(next.status) || next.stderr.length >= serverStderrRef.current.length) {
          replaceSavedStream("stderr", next.stderr);
        }
        if (isTerminal(next.status)) {
          setServerLogLineCount(
            logLineCount(unifiedLogContent(next.stdout, next.stderr, next.error)),
          );
        }
        setLiveStdout(retainLiveLines(next.stdout));
        setLiveStderr(retainLiveLines(next.stderr));
        if (isTerminal(next.status)) {
          streamRef.current?.close();
          // The final result is authoritative: reload the full detail.
          api
            .getExecution(initial.id)
            .then((detail) => applyDetail(generation, detail))
            .catch((error) => {
              if (generation !== generationRef.current) {
                return;
              }
              onErrorRef.current(errorMessage(error));
            });
        }
      },
      onLog(event) {
        if (generation !== generationRef.current) {
          return;
        }
        if (event.stream === "stdout") {
          appendSavedStream("stdout", event.chunk);
          setLiveStdout((current) => retainLiveLines(current + event.chunk));
        } else {
          appendSavedStream("stderr", event.chunk);
          setLiveStderr((current) => retainLiveLines(current + event.chunk));
        }
      },
      onLogSnapshot(event) {
        if (generation !== generationRef.current) {
          return;
        }
        if (event.stream === "stdout") {
          replaceSavedStream("stdout", event.content);
          setLiveStdout(retainLiveLines(event.content));
        } else {
          replaceSavedStream("stderr", event.content);
          setLiveStderr(retainLiveLines(event.content));
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
        if (generation !== generationRef.current) {
          return;
        }
        streamRef.current?.close();
        convergeOnFinalResult(generation, initial.id, FALLBACK_POLICY.maxPolls);
      },
      onError(message) {
        if (generation !== generationRef.current) {
          return;
        }
        onErrorRef.current(message);
      },
    });
  }

  return {
    execution,
    liveStdout,
    liveStderr,
    serverLogLineCount,
    fallbackExhausted,
    watch,
    stop,
  };
}
