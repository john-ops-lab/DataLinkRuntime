import { expect, it, vi } from "vitest";

import { ApiError, api } from "./api";
import { userErrorMessage } from "./user-message";

it("keeps the primary error in Chinese and exposes only the stable code for an English upstream error", () => {
  expect(
    userErrorMessage(
      new ApiError(502, "ai_provider_unreachable", "The AI provider could not be reached"),
      "AI 服务请求失败",
    ),
  ).toBe("AI 服务请求失败（错误码：ai_provider_unreachable）");
});

it("preserves an existing Chinese domain message with its stable code", () => {
  expect(userErrorMessage(new ApiError(409, "adapter_name_conflict", "适配器名称已存在"))).toBe(
    "适配器名称已存在（错误码：adapter_name_conflict）",
  );
});

it("keeps bounded Retry-After feedback separate from the server body", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({ detail: { code: "adapter_queue_full", message: "opaque server text" } }),
      { status: 429, headers: { "Content-Type": "application/json", "Retry-After": "7" } },
    ),
  );
  vi.stubGlobal("fetch", fetchMock);

  await expect(api.getSystemLocale()).rejects.toMatchObject({
    status: 429,
    code: "adapter_queue_full",
    params: { retry_after: 7 },
  });
  expect(
    userErrorMessage(new ApiError(429, "adapter_queue_full", "opaque server text", { retry_after: 7 })),
  ).toBe("适配器排队容量已满，请稍后重试 请在 7 秒后重试（错误码：adapter_queue_full）");
  vi.unstubAllGlobals();
});

it("keeps stable errors when a lightweight response has no headers", async () => {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: false,
    status: 503,
    json: async () => ({ detail: { code: "runtime_capacity_full", message: "capacity is full" } }),
  });
  vi.stubGlobal("fetch", fetchMock);

  await expect(api.getSystemLocale()).rejects.toMatchObject({
    status: 503,
    code: "runtime_capacity_full",
    params: {},
  });
  vi.unstubAllGlobals();
});
