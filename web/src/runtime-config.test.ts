import { expect, it } from "vitest";
import { hasUnsafeRuntimeConfigNumbers, parseRuntimeConfigDraft, runtimeConfigSnapshot } from "./runtime-config";

it.each(["9007199254740993", "-9007199254740993", "1e400", "-1e400"])(
  "rejects nested numbers outside the browser's safe representation: %s", literal => {
    const text = `{"nested":[{"value":${literal}}]}`;
    expect(parseRuntimeConfigDraft(text).kind).toBe("unsafe_number");
  },
);

it("allows safe integers, finite decimals and numeric strings without coercion", () => {
  const value = { edges: [Number.MAX_SAFE_INTEGER, Number.MIN_SAFE_INTEGER, 0.1, -1.25, "9007199254740993"] };
  expect(parseRuntimeConfigDraft(JSON.stringify(value))).toEqual({ kind: "valid", value });
  expect(hasUnsafeRuntimeConfigNumbers(NaN)).toBe(true);
});

it.each(["{broken", "null", "[]", '"legacy"'])("keeps invalid or non-object draft %s invalid", text => {
  expect(parseRuntimeConfigDraft(text)).toEqual({ kind: "invalid" });
});

it("retains the unsafe source fact before stringify turns decoded Infinity into null", () => {
  const value = JSON.parse(`{"nested":[${"1" + "0".repeat(400)}]}`);
  const snapshot = runtimeConfigSnapshot(value);
  expect(snapshot.runtimeConfigUnsafeNumbers).toBe(true);
  expect(JSON.parse(snapshot.runtimeConfigText)).toEqual({ nested: [null] });
  expect(value.nested[0]).toBe(Infinity);
});

it("checks wide arrays without spreading them into function arguments", () => {
  const values = Array.from({ length: 150_000 }, () => 1);
  values[values.length - 1] = Number.MAX_SAFE_INTEGER + 1;
  expect(hasUnsafeRuntimeConfigNumbers({ values })).toBe(true);
});
