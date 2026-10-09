import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useOverlayFocus } from "./useOverlayFocus";

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

it("returns to the fallback when a disappearing menu leaves body focused", () => {
  vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
  const fallback = document.createElement("button");
  fallback.id = "focus-fallback";
  document.body.append(fallback);
  expect(document.activeElement).toBe(document.body);
  const { result } = renderHook(() => useOverlayFocus(true, "#focus-fallback"));
  act(() => result.current(false));
  expect(document.activeElement).toBe(fallback);
});

it("restores a mounted trigger and uses navigation when that trigger was removed", () => {
  vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
  const trigger = document.createElement("button");
  const navigation = document.createElement("a");
  navigation.href = "/adapters";
  document.body.append(trigger, navigation);
  trigger.focus();
  const { result } = renderHook(() => useOverlayFocus(true, 'a[href="/adapters"]'));
  navigation.focus();
  act(() => result.current(false));
  expect(document.activeElement).toBe(trigger);
  trigger.remove();
  act(() => result.current(false));
  expect(document.activeElement).toBe(navigation);
});
