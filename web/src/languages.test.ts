import { expect, it } from "vitest";

import {
  DEPENDENCY_NOTE,
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
  "%s Webhook starter leaves entry Bearer authentication outside code secrets",
  (language) => {
    const starter = WEBHOOK_STARTER_CODE[language];
    expect(starter).not.toContain('context.secrets.get("TOKEN")');
    expect(starter).not.toMatch(/token\s*=\s*/i);
    expect(starter).not.toMatch(/print\(/i);
    expect(starter).not.toMatch(/console\.log/i);
    expect(starter).toContain("Bearer");
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
  "%s webhook starter reads input, logs receipt and returns output",
  (language) => {
    const starter = WEBHOOK_STARTER_CODE[language];
    expect(starter).toContain("input");
    expect(starter).toContain("return");
    expect(starter).toContain("context.logger");
    expect(starter).toContain("收到 Webhook 请求");
    // The starter does not force a second secret-dependent boundary.
    expect(starter.match(/context\.logger/g)).toHaveLength(1);
    expect(starter.indexOf("收到 Webhook 请求")).toBeLessThan(
      starter.indexOf("return"),
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
  ["python", "Python 依赖", "requests==2.32.3"],
  ["javascript", "JavaScript 依赖", "axios@1.7.7"],
  ["java", "Java 依赖", "org.apache.commons:commons-lang3:3.17.0"],
] as const)(
  "%s dependency UI label and placeholder follow the M5.5.8 wording",
  (language, label, placeholderMarker) => {
    const ui = DEPENDENCY_UI[language];
    expect(ui.label).toBe(label);
    expect(ui.placeholder).toContain(placeholderMarker);
    // 三种语言统一提示"回车换行，每行写一个依赖"。
    expect(ui.placeholder).toContain("回车换行，每行写一个依赖");
  },
);

it("dependency note explains install timing, System Settings source and empty behavior", () => {
  expect(DEPENDENCY_NOTE).toContain("Worker 执行前会安装这些依赖");
  expect(DEPENDENCY_NOTE).toContain("系统设置");
  expect(DEPENDENCY_NOTE).toContain("不填写则平台不会额外检查依赖是否齐全");
});
