import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useOverlayFocus } from "./useOverlayFocus";

function mockFrames() {
  const frames = new Map<number, FrameRequestCallback>();
  let next = 0;
  vi.spyOn(window, "requestAnimationFrame").mockImplementation(callback => {
    frames.set(++next, callback);
    return next;
  });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(id => { frames.delete(id); });
  return () => act(() => {
    const callbacks = [...frames.values()];
    frames.clear();
    callbacks.forEach(callback => callback(0));
  });
}

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

it("returns to the fallback when a disappearing menu leaves body focused", () => {
  const flush = mockFrames();
  vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
  const fallback = document.createElement("button");
  fallback.id = "focus-fallback";
  document.body.append(fallback);
  expect(document.activeElement).toBe(document.body);
  const { result } = renderHook(() => useOverlayFocus(true, "#focus-fallback"));
  act(() => result.current(false));
  flush();
  expect(document.activeElement).toBe(fallback);
});

it("restores a mounted trigger and uses navigation when that trigger was removed", () => {
  const flush = mockFrames();
  vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
  const trigger = document.createElement("button");
  const navigation = document.createElement("a");
  navigation.href = "/adapters";
  document.body.append(trigger, navigation);
  trigger.focus();
  const { result } = renderHook(() => useOverlayFocus(true, 'a[href="/adapters"]'));
  navigation.focus();
  act(() => result.current(false));
  flush();
  expect(document.activeElement).toBe(trigger);
  trigger.remove();
  act(() => result.current(false));
  flush();
  expect(document.activeElement).toBe(navigation);
});

it("restores after the drawer's own focus callback overwrites the target", () => {
  const flush = mockFrames();
  vi.spyOn(HTMLElement.prototype, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
  const trigger = document.createElement("button");
  const drawerSentinel = document.createElement("div");
  drawerSentinel.tabIndex = -1;
  document.body.append(trigger, drawerSentinel);
  trigger.focus();
  const { result } = renderHook(() => useOverlayFocus(true, "button"));
  act(() => result.current(false));
  drawerSentinel.focus();
  flush();
  expect(document.activeElement).toBe(trigger);
});
