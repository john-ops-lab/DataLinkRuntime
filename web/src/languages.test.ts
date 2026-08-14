import { expect, it } from "vitest";

import { STARTER_CODE } from "./languages";

it.each(["python", "javascript", "java"] as const)(
  "%s Task starter reads input, returns output and logs both boundaries",
  (language) => {
    const starter = STARTER_CODE[language];
    expect(starter).toContain("input");
    expect(starter).toContain("return");
    expect(starter).toContain("context.logger");
    expect(starter).toContain("任务开始");
    expect(starter).toContain("任务结束");
    expect(starter.indexOf("任务开始")).toBeLessThan(starter.indexOf("return"));
    expect(starter.indexOf("return")).toBeLessThan(starter.indexOf("任务结束"));
  },
);
