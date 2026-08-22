import { expect, it } from "vitest";

import { tailLogLines, unifiedLogContent } from "./unified-log";

it("keeps legacy errors in one unified stream without duplicating new log errors", () => {
  expect(unifiedLogContent("[time] [ERROR] failed\n", "", "failed")).toBe(
    "[time] [ERROR] failed\n",
  );
  expect(unifiedLogContent("", "legacy stderr\n", "legacy failure")).toBe(
    "legacy stderr\nlegacy failure",
  );
});

it("keeps the full history content when no browser window cap is requested", () => {
  const content = Array.from({ length: 501 }, (_, index) => `line-${index}`).join("\n") + "\n";

  expect(content).toContain("line-0\n");
  expect(content).toContain("line-500\n");
  expect(tailLogLines(content, 2000)).toBe(content);
  expect(content.endsWith("\n")).toBe(true);
});
