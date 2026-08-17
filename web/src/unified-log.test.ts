import { expect, it } from "vitest";

import { HISTORY_LOG_MAX_LINES, tailLogLines, unifiedLogContent } from "./unified-log";

it("keeps legacy errors in one unified stream without duplicating new log errors", () => {
  expect(unifiedLogContent("[time] [ERROR] failed\n", "", "failed")).toBe(
    "[time] [ERROR] failed\n",
  );
  expect(unifiedLogContent("", "legacy stderr\n", "legacy failure")).toBe(
    "legacy stderr\nlegacy failure",
  );
});

it("returns only the newest history lines and preserves the line ending", () => {
  const content =
    Array.from({ length: HISTORY_LOG_MAX_LINES + 1 }, (_, index) => `line-${index}`).join("\n") +
    "\n";
  const tail = tailLogLines(content, HISTORY_LOG_MAX_LINES);

  expect(tail).toMatch(/^line-1\n/);
  expect(tail).toContain(`line-${HISTORY_LOG_MAX_LINES}\n`);
  expect(tail).not.toContain("line-0");
  expect(tail.endsWith("\n")).toBe(true);
  expect(tail.split("\n").filter((line) => line !== "").length).toBe(HISTORY_LOG_MAX_LINES);
});
