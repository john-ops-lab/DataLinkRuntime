import { describe, expect, it, vi } from "vitest";

import {
  notifyCredentialCatalogChanged,
  subscribeCredentialCatalog,
} from "./credential-catalog";

describe("credential-catalog", () => {
  it("notifies every subscriber once per change and stops after unsubscribe", () => {
    const first = vi.fn();
    const second = vi.fn();
    const unsubscribeFirst = subscribeCredentialCatalog(first);
    const unsubscribeSecond = subscribeCredentialCatalog(second);

    notifyCredentialCatalogChanged();
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);

    unsubscribeFirst();
    notifyCredentialCatalogChanged();
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(2);

    unsubscribeSecond();
    notifyCredentialCatalogChanged();
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(2);
  });

  it("keeps notifying the remaining subscribers when a subscription is removed mid-iteration", () => {
    const third = vi.fn();
    const removeThird = subscribeCredentialCatalog(third);
    const first = vi.fn(() => {
      removeThird();
    });
    const removeFirst = subscribeCredentialCatalog(first);

    notifyCredentialCatalogChanged();
    expect(first).toHaveBeenCalledTimes(1);
    expect(third).toHaveBeenCalledTimes(1);
    removeFirst();
  });

  it("refreshes subscribed metadata on focus and releases the browser listeners", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeCredentialCatalog(listener);
    window.dispatchEvent(new Event("focus"));
    document.dispatchEvent(new Event("visibilitychange"));
    expect(listener).toHaveBeenCalledTimes(2);
    unsubscribe();
    window.dispatchEvent(new Event("focus"));
    expect(listener).toHaveBeenCalledTimes(2);
  });
});
