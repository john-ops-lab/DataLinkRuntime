/** M5.5.10 unified log helpers shared by the Workbench live-log Tab and the
 * execution history detail. */

export const LIVE_LOG_MAX_LINES = 2000;
export const HISTORY_LOG_MAX_LINES = 500;

/** Merged display content: the unified stdout-channel stream plus any legacy
 * stderr content (pre-M5.5.10 rows), so one view always carries everything. */
export function unifiedLogContent(
  stdout: string,
  stderr: string,
  error: string | null = null,
): string {
  let content: string;
  if (stderr === "") {
    content = stdout;
  } else if (stdout === "") {
    content = stderr;
  } else {
    content = `${stdout}${stdout.endsWith("\n") ? "" : "\n"}${stderr}`;
  }
  // New executions already put platform failures in the unified stdout stream.
  // Append the legacy error field only when it is not already represented, so
  // old failed executions still have one complete log without duplicate text.
  if (error !== null && error !== "" && !content.includes(error)) {
    content = `${content}${content !== "" && !content.endsWith("\n") ? "\n" : ""}${error}`;
  }
  return content;
}

/** Keep the newest logical lines while preserving a final newline when present. */
export function tailLogLines(content: string, maxLines: number): string {
  if (content === "" || maxLines <= 0) {
    return "";
  }
  const hasTrailingNewline = content.endsWith("\n");
  const lines = content.split("\n");
  if (hasTrailingNewline) {
    lines.pop();
  }
  if (lines.length <= maxLines) {
    return content;
  }
  const tail = lines.slice(-maxLines).join("\n");
  return hasTrailingNewline ? `${tail}\n` : tail;
}

/** One unified-log line's capture-time prefix, e.g.
 * "[2026-08-17 10:21:03] ..." (see worker LOG_LINE_TIME_FORMAT). */
const LOG_LINE_TIME_PATTERN = /^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\]/;

/** M5.5.13: display label for a log context snippet, derived from the
 * capture-time prefixes of its first and last selected lines (the same
 * browser-visible masked text). Returns null when no prefixes are found, so
 * the caller can fall back to the line range. */
export function logSnippetTimeLabel(text: string): string | null {
  const lines = text.split("\n");
  const first = lines.find((line) => LOG_LINE_TIME_PATTERN.test(line));
  const last = [...lines].reverse().find((line) => LOG_LINE_TIME_PATTERN.test(line));
  if (first === undefined || last === undefined) {
    return null;
  }
  const firstMatch = LOG_LINE_TIME_PATTERN.exec(first);
  const lastMatch = LOG_LINE_TIME_PATTERN.exec(last);
  if (firstMatch === null || lastMatch === null) {
    return null;
  }
  const firstDate = firstMatch[1];
  const firstTime = firstMatch[2];
  const lastDate = lastMatch[1];
  const lastTime = lastMatch[2];
  if (firstDate === lastDate) {
    return `${firstTime}–${lastTime}`;
  }
  return `${firstDate} ${firstTime}–${lastDate} ${lastTime}`;
}
