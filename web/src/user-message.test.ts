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

it("maps sandbox resource codes through the bilingual domain catalog", () => {
  expect(userErrorMessage(new ApiError(200, "resource_exceeded_memory", "opaque"))).toBe(
    "执行超出内存限制（错误码：resource_exceeded_memory）",
  );
  expect(
    userErrorMessage(
      new ApiError(200, "resource_exceeded_memory", "opaque"),
      undefined,
      "en",
    ),
  ).toBe("The execution exceeded its memory limit (Error code: resource_exceeded_memory)");
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

it("distinguishes a provider HTTP response from network failure in both locales", () => {
  expect(userErrorMessage(new ApiError(502, "ai_provider_http_error", "opaque", { http_status: 429 })))
    .toBe("模型服务返回 HTTP 错误，请检查服务状态或请求配置 上游 HTTP 429（错误码：ai_provider_http_error）");
  expect(userErrorMessage(
    new ApiError(502, "ai_provider_http_error", "opaque", { http_status: 503 }), undefined, "en",
  )).toBe("The model service returned an HTTP error. Check the service status or request configuration Upstream HTTP 503 (Error code: ai_provider_http_error)");
  expect(userErrorMessage(
    new ApiError(502, "ai_provider_http_error", "opaque", { http_status: 502 }), "AI request failed", "en",
  )).toBe("AI request failed Upstream HTTP 502 (Error code: ai_provider_http_error)");
});

it("does not expose malformed provider HTTP status params or raw upstream detail", () => {
  for (const http_status of [true, false, "PRIVATE_RAW_EXCEPTION", "502", 99, 600, 502.5, null]) {
    const message = userErrorMessage(
      new ApiError(502, "ai_provider_http_error", "PRIVATE_RAW_BODY_URL_HEADER", { http_status }),
      undefined, "en",
    );
    expect(message).not.toContain("Upstream HTTP");
    expect(message).not.toContain("PRIVATE_RAW");
  }
});

it("explains that prepared attachments require a tool-capable configuration in both locales", () => {
  const error = new ApiError(422, "ai_attachment_tools_unsupported", "PRIVATE_RAW_BODY");
  expect(userErrorMessage(error)).toBe(
    "当前模型配置不支持附件读取：请更换为支持工具调用的模型配置后再发送附件（错误码：ai_attachment_tools_unsupported）",
  );
  expect(userErrorMessage(error, undefined, "en")).toBe(
    "The current model configuration cannot read prepared attachments: choose a model configuration with tool support before sending attachments (Error code: ai_attachment_tools_unsupported)",
  );
});
