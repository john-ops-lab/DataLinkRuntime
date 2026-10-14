import { describe, expect, it } from "vitest";
import { JsonInputKeyConflict, normalizeJsonInput } from "./json-input-normalization";

describe("parsed JSON normalization", () => {
  it("preserves characters, structure, types, and special keys", () => {
    const raw = JSON.parse('{"a\\u0000b":["A\\u0000B","\\u0000\\u0000","","A\\\\u0000B","中\\n\\t",0,false,1,true,null],"__proto__":{"constructor":"x"}}');
    const normalized = normalizeJsonInput(raw);
    expect(normalized.removed).toBe(4);
    expect(normalized.value).toEqual(JSON.parse('{"ab":["AB","","","A\\\\u0000B","中\\n\\t",0,false,1,true,null],"__proto__":{"constructor":"x"}}'));
    expect(Object.hasOwn(normalized.value as object, "__proto__")).toBe(true);
    expect(normalizeJsonInput(normalized.value)).toEqual({ value: normalized.value, removed: 0 });
    expect(raw["a\u0000b"][0]).toBe("A\u0000B");
  });
  it("normalizes top level strings and reports nested key collisions", () => {
    expect(normalizeJsonInput("\u0000A\u0000")).toEqual({ value: "A", removed: 2 });
    try {
      normalizeJsonInput({ "a/b~c": [{ "a\u0000b": 1, ab: 2 }] });
      throw new Error("expected conflict");
    } catch (error) {
      expect(error).toBeInstanceOf(JsonInputKeyConflict);
      expect((error as JsonInputKeyConflict).path).toBe("/a~1b~0c/0");
      expect((error as JsonInputKeyConflict).key).toBe("ab");
    }
  });
});
