/** SSE client for the Execution event stream (M3 spec §7.4).
 *
 * The native ``EventSource`` cannot carry a custom Authorization header, so
 * the stream is read with ``fetch()`` + ReadableStream. The admin token is
 * sent as a Bearer header only and must never appear in the URL.
 */

import { getAuthToken, handleUnauthorized } from "./api";
import { currentSystemLocale, i18n } from "./i18n";
import type { Execution } from "./types";

function sseErrorMessage(key: string, params?: Record<string, unknown>): string {
  return i18n.getFixedT(currentSystemLocale(), "common")(key, params);
}

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
  /** Stream ended (EOF or read error) without a terminal event and without
   * an explicit close(); callers must fall back to the authoritative API. */
  onUnexpectedClose?: () => void;
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
 *
 * Every abnormal end except 401 (handled globally) and an explicit
 * ``close()`` reports ``onUnexpectedClose`` — including an initial connect
 * failure or a non-2xx answer — so the caller always falls back to the
 * authoritative execution API instead of staying stuck on a stale state.
 */
export function openExecutionEvents(
  executionId: number,
  handlers: ExecutionEventsHandlers,
): ExecutionEventsHandle {
  const controller = new AbortController();
  let closed = false;

  function fallbackUnlessClosed() {
    if (!closed) {
      handlers.onUnexpectedClose?.();
    }
  }

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
    } catch {
      if (!closed) {
        handlers.onError?.(sseErrorMessage("errors.sse_connect_failed"));
        fallbackUnlessClosed();
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
      handlers.onError?.(sseErrorMessage("errors.sse_connect_failed_http", { status: response.status }));
      fallbackUnlessClosed();
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
    } catch {
      if (!closed) {
        handlers.onError?.(sseErrorMessage("errors.sse_read_failed"));
      }
    }
    // The server only closes the stream after a terminal status, and a
    // terminal event triggers close() on the caller side: any other end of
    // the stream (Control restart, proxy drop, nginx read timeout, read
    // error) is unexpected and must not leave the caller stuck on a stale
    // running state.
    fallbackUnlessClosed();
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
