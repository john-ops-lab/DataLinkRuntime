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
    subscribeCredentialCatalog(first);

    notifyCredentialCatalogChanged();
    expect(first).toHaveBeenCalledTimes(1);
    expect(third).toHaveBeenCalledTimes(1);
  });
});
