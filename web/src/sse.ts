/** SSE client for the Execution event stream (M3 spec §7.4).
 *
 * The native ``EventSource`` cannot carry a custom Authorization header, so
 * the stream is read with ``fetch()`` + ReadableStream. The admin token is
 * sent as a Bearer header only and must never appear in the URL.
 */

import { getAuthToken, handleUnauthorized } from "./api";
import type { Execution } from "./types";

export interface LogEvent {
  stream: "stdout" | "stderr";
  chunk: string;
}

export interface LogSnapshotEvent {
  stream: "stdout" | "stderr";
  content: string;
  truncated: boolean;
}

export interface ExecutionEventsHandlers {
  onExecution?: (execution: Execution) => void;
  onLog?: (event: LogEvent) => void;
  onLogSnapshot?: (event: LogSnapshotEvent) => void;
  onError?: (message: string) => void;
}

export interface ExecutionEventsHandle {
  close: () => void;
}

interface SseEvent {
  event: string;
  data: string;
}

/** Split a raw SSE text buffer into complete events plus the leftover tail. */
export function parseSseBuffer(buffer: string): { events: SseEvent[]; rest: string } {
  const events: SseEvent[] = [];
  let rest = buffer;
  let separator = rest.indexOf("\n\n");
  while (separator !== -1) {
    const block = rest.slice(0, separator);
    rest = rest.slice(separator + 2);
    const parsed: SseEvent = { event: "message", data: "" };
    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) {
        parsed.event = line.slice("event: ".length);
      } else if (line.startsWith("data: ")) {
        parsed.data = line.slice("data: ".length);
      }
      // ":" comment lines (keepalive) carry no data and are dropped.
    }
    if (parsed.data !== "") {
      events.push(parsed);
    }
    separator = rest.indexOf("\n\n");
  }
  return { events, rest };
}

/** Open the event stream of one Execution; returns a handle to close it.
 *
 * The server closes the stream after a terminal status; the reader then
 * finishes naturally. ``close()`` aborts the fetch for early teardown
 * (adapter switch, unmount) and is idempotent.
 */
export function openExecutionEvents(
  executionId: number,
  handlers: ExecutionEventsHandlers,
): ExecutionEventsHandle {
  const controller = new AbortController();
  let closed = false;

  async function run() {
    const token = getAuthToken();
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (token !== null) {
      headers.Authorization = `Bearer ${token}`;
    }
    let response: Response;
    try {
      response = await fetch(`/api/executions/${executionId}/events`, {
        headers,
        signal: controller.signal,
      });
    } catch (error) {
      if (!closed) {
        handlers.onError?.(error instanceof Error ? error.message : "实时事件连接失败");
      }
      return;
    }
    if (closed) {
      return;
    }
    if (response.status === 401) {
      handleUnauthorized();
      return;
    }
    if (!response.ok || response.body === null) {
      handlers.onError?.(`实时事件连接失败（HTTP ${response.status}）`);
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const { events, rest } = parseSseBuffer(buffer);
        buffer = rest;
        for (const event of events) {
          dispatch(event, handlers);
        }
      }
    } catch (error) {
      if (!closed) {
        handlers.onError?.(error instanceof Error ? error.message : "实时事件读取失败");
      }
    }
  }

  void run();

  return {
    close() {
      closed = true;
      controller.abort();
    },
  };
}

function dispatch(event: SseEvent, handlers: ExecutionEventsHandlers): void {
  let payload: unknown;
  try {
    payload = JSON.parse(event.data);
  } catch {
    return;
  }
  if (event.event === "execution") {
    handlers.onExecution?.(payload as Execution);
    return;
  }
  if (event.event === "log") {
    handlers.onLog?.(payload as LogEvent);
    return;
  }
  if (event.event === "log_snapshot") {
    handlers.onLogSnapshot?.(payload as LogSnapshotEvent);
  }
}
