import { afterEach, describe, expect, it } from "vitest";

import {
  applySystemLocale,
  currentSystemLocale,
  DEFAULT_SYSTEM_LOCALE,
  i18n,
  resources,
} from ".";

function leafKeys(value: unknown, prefix = ""): string[] {
  if (typeof value !== "object" || value === null) {
    return [prefix];
  }
  return Object.entries(value).flatMap(([key, child]) =>
    leafKeys(child, prefix === "" ? key : `${prefix}.${key}`),
  );
}

afterEach(async () => {
  await applySystemLocale(DEFAULT_SYSTEM_LOCALE);
});

describe("locale infrastructure", () => {
  it("keeps every bundled namespace key set identical", () => {
    for (const namespace of Object.keys(resources[DEFAULT_SYSTEM_LOCALE])) {
      const zhKeys = leafKeys(resources["zh-CN"][namespace as keyof typeof resources.en]);
      const enKeys = leafKeys(resources.en[namespace as keyof typeof resources.en]);
      expect(enKeys, `${namespace} English keys`).toEqual(zhKeys);
    }
  });

  it("uses a safe human fallback instead of rendering a raw missing key", () => {
    const missingKey = "common.__missing_wave_1_key__";
    const translated = i18n.t(missingKey);

    expect(translated).not.toBe(missingKey);
    expect(translated).toBe("暂不可用");
  });

  it("switches immediately between the two supported locales", async () => {
    await applySystemLocale("en");
    expect(currentSystemLocale()).toBe("en");
    expect(i18n.t("auth.loginTitle")).toBe("Welcome to the DLR Console");

    await applySystemLocale("zh-CN");
    expect(currentSystemLocale()).toBe("zh-CN");
    expect(i18n.t("auth.loginTitle")).toBe("欢迎登录 DLR 控制台");
  });
});
