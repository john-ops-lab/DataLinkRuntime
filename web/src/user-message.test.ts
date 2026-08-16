import { expect, it } from "vitest";

import { ApiError } from "./api";
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
