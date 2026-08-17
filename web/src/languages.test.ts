import { expect, it } from "vitest";

import {
  DEPENDENCY_UI,
  STARTER_CODE,
  TASK_STARTER_CODE,
  WEBHOOK_STARTER_CODE,
} from "./languages";

it.each(["python", "javascript", "java"] as const)(
  "%s Task starter reads input, returns output and logs both boundaries",
  (language) => {
    const starter = TASK_STARTER_CODE[language];
    expect(starter).toContain("input");
    expect(starter).toContain("return");
    expect(starter).toContain("context.logger");
    expect(starter).toContain("任务开始");
    expect(starter).toContain("任务结束");
    expect(starter.indexOf("任务开始")).toBeLessThan(starter.indexOf("return"));
    expect(starter.indexOf("return")).toBeLessThan(starter.indexOf("任务结束"));
  },
);

it.each(["python", "javascript", "java"] as const)(
  "%s Task starter really reads the PASSWORD binding and never leaks it",
  (language) => {
    const starter = TASK_STARTER_CODE[language];
    expect(starter).toContain('context.secrets.get("PASSWORD")');
    // 代码里只有“读取”示例，绝无真实密码、打印或返回 Secret。
    expect(starter).not.toMatch(/password\s*=\s*["']/i);
    expect(starter).not.toMatch(/print\(/i);
    expect(starter).not.toMatch(/console\.log/i);
    expect(starter).toContain("不要把真实密码直接写进代码");
  },
);

it.each(["python", "javascript", "java"] as const)(
  "%s Webhook starter really reads the TOKEN binding and never leaks it",
  (language) => {
    const starter = WEBHOOK_STARTER_CODE[language];
    expect(starter).toContain('context.secrets.get("TOKEN")');
    expect(starter).not.toMatch(/token\s*=\s*["']/i);
    expect(starter).not.toMatch(/print\(/i);
    expect(starter).not.toMatch(/console\.log/i);
    expect(starter).toContain("不要把真实 Token 直接写进代码");
  },
);

it.each(["python", "javascript", "java"] as const)(
  "%s legacy starter is not polluted by Task log semantics",
  (language) => {
    expect(STARTER_CODE[language]).not.toContain("任务开始");
    expect(STARTER_CODE[language]).not.toContain("任务结束");
  },
);

it.each(["python", "javascript", "java"] as const)(
  "%s webhook starter reads input, logs Webhook start/end boundaries and returns output in order",
  (language) => {
    const starter = WEBHOOK_STARTER_CODE[language];
    expect(starter).toContain("input");
    expect(starter).toContain("return");
    expect(starter).toContain("context.logger");
    expect(starter).toContain("收到 Webhook 请求");
    expect(starter).toContain("处理完 Webhook 请求");
    // logger is emitted exactly once at each boundary
    expect(starter.match(/context\.logger/g)).toHaveLength(2);
    // start boundary -> return -> end boundary
    expect(starter.indexOf("收到 Webhook 请求")).toBeLessThan(
      starter.indexOf("return"),
    );
    expect(starter.indexOf("return")).toBeLessThan(
      starter.indexOf("处理完 Webhook 请求"),
    );
  },
);

it.each(["python", "javascript", "java"] as const)(
  "%s webhook starter is not polluted by Task log semantics",
  (language) => {
    const starter = WEBHOOK_STARTER_CODE[language];
    expect(starter).not.toContain("任务开始");
    expect(starter).not.toContain("任务结束");
  },
);

it.each(["python", "javascript", "java"] as const)(
  "%s dependency UI config has non-empty label and placeholder",
  (language) => {
    const ui = DEPENDENCY_UI[language];
    expect(ui.label.trim().length).toBeGreaterThan(0);
    expect(ui.placeholder.trim().length).toBeGreaterThan(0);
  },
);

it.each([
  ["python", "Python", "requests=="],
  ["javascript", "npm", "axios@"],
  ["java", "Maven", "okhttp"],
] as const)(
  "%s dependency UI label and placeholder match the language",
  (language, labelMarker, placeholderMarker) => {
    const ui = DEPENDENCY_UI[language];
    expect(ui.label).toContain(labelMarker);
    expect(ui.placeholder).toContain(placeholderMarker);
  },
);
