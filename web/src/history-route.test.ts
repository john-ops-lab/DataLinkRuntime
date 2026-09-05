import { afterEach, describe, expect, it, vi } from "vitest";

import {
  appRouteFromPath,
  backBrowserLocation,
  browserLocationSnapshot,
  pushBrowserLocation,
  replaceBrowserLocation,
  setBrowserLocationBlocker,
  subscribeToBrowserLocation,
  templatePath,
} from "./history-route";

const HISTORY_EPOCH_KEY = "__datalink_runtime_history_epoch_v1__";

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

function waitForPopstates(count: number): Promise<void> {
  return new Promise((resolve) => {
    let observed = 0;
    const listener = () => {
      observed += 1;
      if (observed === count) {
        window.removeEventListener("popstate", listener);
        resolve();
      }
    };
    window.addEventListener("popstate", listener);
  });
}

describe("appRouteFromPath", () => {
  it.each([
    ["/", { section: "adapters", settingsCategory: null }],
    ["/adapters", { section: "adapters", settingsCategory: null }],
    ["/templates", { section: "templates", scenarioSlug: null }],
    ["/templates/", { section: "templates", scenarioSlug: null }],
    ["/templates/rest-single-request", { section: "templates", scenarioSlug: "rest-single-request" }],
    ["/settings/system-status", { section: "adapters", settingsCategory: "system-status" }],
  ])("parses %s", (path, expected) => {
    expect(appRouteFromPath(path)).toEqual(expected);
  });

  it.each([
    "/templates/../secret",
    "/templates/%2Fetc",
    "/templates/UPPERCASE",
    "/templates/a/b",
    "/unknown",
  ])("fails closed to the Adapter surface for %s", (path) => {
    expect(appRouteFromPath(path)).toEqual({ section: "adapters", settingsCategory: null });
  });
});

it("builds encoded template paths", () => {
  expect(templatePath()).toBe("/templates");
  expect(templatePath("rest-single-request")).toBe("/templates/rest-single-request");
});

it("publishes History API navigation to subscribers", () => {
  const listener = vi.fn();
  const unsubscribe = subscribeToBrowserLocation(listener);

  pushBrowserLocation("/templates");

  expect(browserLocationSnapshot()).toBe("/templates");
  expect(listener).toHaveBeenCalledTimes(1);
  unsubscribe();
});

it("replaces an in-app location without adding a Back-stack entry", async () => {
  window.history.replaceState(null, "", "/adapters");
  const listener = vi.fn();
  const unsubscribe = subscribeToBrowserLocation(listener);
  pushBrowserLocation("/templates");

  replaceBrowserLocation("/templates/rest-single-request", { source: "direct" });
  expect(browserLocationSnapshot()).toBe("/templates/rest-single-request");
  expect(window.history.state).toEqual(expect.objectContaining({ source: "direct" }));
  expect(listener).toHaveBeenCalledTimes(2);

  const backToAdapters = waitForPopstates(1);
  backBrowserLocation();
  await backToAdapters;
  expect(browserLocationSnapshot()).toBe("/adapters");
  unsubscribe();
});

it("compensates a blocked native Back without truncating the Forward branch", async () => {
  window.history.replaceState({ source: "a" }, "", "/a");
  const listener = vi.fn();
  const unsubscribe = subscribeToBrowserLocation(listener);
  pushBrowserLocation("/b", { source: "b" });
  pushBrowserLocation("/c", { source: "c" });

  const backToB = waitForPopstates(1);
  window.history.back();
  await backToB;
  expect(browserLocationSnapshot()).toBe("/b");

  const blocker = vi.fn(() => false);
  const removeBlocker = setBrowserLocationBlocker(blocker);
  const blockedBackAndCompensation = waitForPopstates(2);
  window.history.back();
  await blockedBackAndCompensation;

  expect(blocker).toHaveBeenCalledWith("/a");
  expect(browserLocationSnapshot()).toBe("/b");
  expect(window.history.state).toEqual(expect.objectContaining({ source: "b" }));
  expect(listener).toHaveBeenCalledTimes(3);

  removeBlocker();
  const forwardToC = waitForPopstates(1);
  window.history.forward();
  await forwardToC;
  expect(browserLocationSnapshot()).toBe("/c");
  expect(window.history.state).toEqual(expect.objectContaining({ source: "c" }));
  expect(listener).toHaveBeenCalledTimes(4);
  unsubscribe();
});

it("restores the accepted entry in place for a blocked legacy Back", async () => {
  window.history.replaceState(null, "", "/legacy-a");
  window.history.pushState({ legacy: true }, "", "/legacy-b");
  const historyLength = window.history.length;
  const listener = vi.fn();
  const unsubscribe = subscribeToBrowserLocation(listener);
  const blocker = vi.fn(() => false);
  const removeBlocker = setBrowserLocationBlocker(blocker);

  const blockedBack = waitForPopstates(1);
  window.history.back();
  await blockedBack;

  expect(blocker).toHaveBeenCalledWith("/legacy-a");
  expect(browserLocationSnapshot()).toBe("/legacy-b");
  expect(window.history.state).toEqual(expect.objectContaining({ legacy: true }));
  expect(window.history.length).toBe(historyLength);
  expect(listener).not.toHaveBeenCalled();

  removeBlocker();
  unsubscribe();
});

it("starts a new epoch when approved Back reaches a legacy entry", async () => {
  window.history.replaceState({ source: "legacy-a" }, "", "/legacy-a");
  window.history.pushState({ source: "legacy-b" }, "", "/legacy-b");
  const listener = vi.fn();
  const unsubscribe = subscribeToBrowserLocation(listener);
  const startingEpoch = (window.history.state as Record<string, unknown>)[HISTORY_EPOCH_KEY];

  const approvedBack = waitForPopstates(1);
  backBrowserLocation();
  await approvedBack;

  const legacyTargetEpoch = (window.history.state as Record<string, unknown>)[HISTORY_EPOCH_KEY];
  expect(browserLocationSnapshot()).toBe("/legacy-a");
  expect(legacyTargetEpoch).not.toBe(startingEpoch);
  expect(listener).toHaveBeenCalledTimes(1);

  pushBrowserLocation("/from-legacy");
  expect((window.history.state as Record<string, unknown>)[HISTORY_EPOCH_KEY]).toBe(
    legacyTargetEpoch,
  );
  expect(listener).toHaveBeenCalledTimes(2);
  unsubscribe();
});

it("blocks a legacy Forward in place without truncating a later Forward entry", async () => {
  expect((window as unknown as { navigation?: unknown }).navigation).toBeUndefined();
  window.history.replaceState({ source: "legacy-a" }, "", "/legacy-a");
  window.history.pushState({ source: "legacy-b" }, "", "/legacy-b");
  window.history.pushState({ source: "legacy-c" }, "", "/legacy-c");
  window.history.pushState({ source: "legacy-d" }, "", "/legacy-d");

  const moveToB = waitForPopstates(1);
  window.history.go(-2);
  await moveToB;
  expect(browserLocationSnapshot()).toBe("/legacy-b");

  // Mounting at B models a reload whose adjacent Forward entries predate the
  // Console's internal history metadata.
  const historyLength = window.history.length;
  const listener = vi.fn();
  const unsubscribe = subscribeToBrowserLocation(listener);
  const blocker = vi.fn(() => false);
  const removeBlocker = setBrowserLocationBlocker(blocker);

  const blockedForward = waitForPopstates(1);
  window.history.forward();
  await blockedForward;

  expect(blocker).toHaveBeenCalledWith("/legacy-c");
  expect(browserLocationSnapshot()).toBe("/legacy-b");
  expect(window.history.state).toEqual(expect.objectContaining({ source: "legacy-b" }));
  expect(window.history.length).toBe(historyLength);
  expect(listener).not.toHaveBeenCalled();

  // Rejecting an unindexed traversal necessarily replaces C's current slot
  // with the accepted B state. It does not truncate the later D entry.
  removeBlocker();
  const forwardToD = waitForPopstates(1);
  window.history.forward();
  await forwardToD;
  expect(browserLocationSnapshot()).toBe("/legacy-d");
  expect(window.history.state).toEqual(expect.objectContaining({ source: "legacy-d" }));
  expect(listener).toHaveBeenCalledTimes(1);
  unsubscribe();
});

it("keeps an accepted legacy target conservative for a later blocked traversal", async () => {
  window.history.replaceState({ source: "legacy-a" }, "", "/legacy-a");
  window.history.pushState({ source: "legacy-b" }, "", "/legacy-b");
  window.history.pushState({ source: "legacy-c" }, "", "/legacy-c");

  const moveToB = waitForPopstates(1);
  window.history.back();
  await moveToB;
  const historyLength = window.history.length;
  const listener = vi.fn();
  const unsubscribe = subscribeToBrowserLocation(listener);

  const acceptedForward = waitForPopstates(1);
  window.history.forward();
  await acceptedForward;
  expect(browserLocationSnapshot()).toBe("/legacy-c");
  expect(listener).toHaveBeenCalledTimes(1);

  const blocker = vi.fn(() => false);
  const removeBlocker = setBrowserLocationBlocker(blocker);
  const blockedBack = waitForPopstates(1);
  window.history.back();
  await blockedBack;

  expect(blocker).toHaveBeenCalledWith("/legacy-b");
  expect(browserLocationSnapshot()).toBe("/legacy-c");
  expect(window.history.state).toEqual(expect.objectContaining({ source: "legacy-c" }));
  expect(window.history.length).toBe(historyLength);
  expect(listener).toHaveBeenCalledTimes(1);

  removeBlocker();
  unsubscribe();
});

it("compensates a blocked native Forward and still allows it to be retried", async () => {
  window.history.replaceState({ source: "a" }, "", "/a");
  const unsubscribe = subscribeToBrowserLocation(() => undefined);
  pushBrowserLocation("/b", { source: "b" });
  pushBrowserLocation("/c", { source: "c" });
  const backToB = waitForPopstates(1);
  window.history.back();
  await backToB;

  const blocker = vi.fn(() => false);
  const removeBlocker = setBrowserLocationBlocker(blocker);
  const blockedForwardAndCompensation = waitForPopstates(2);
  window.history.forward();
  await blockedForwardAndCompensation;

  expect(blocker).toHaveBeenCalledWith("/c");
  expect(browserLocationSnapshot()).toBe("/b");
  expect(window.history.state).toEqual(expect.objectContaining({ source: "b" }));

  removeBlocker();
  const retryForward = waitForPopstates(1);
  window.history.forward();
  await retryForward;
  expect(browserLocationSnapshot()).toBe("/c");
  unsubscribe();
});
