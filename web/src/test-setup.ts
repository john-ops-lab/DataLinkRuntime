import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom does not implement matchMedia; antd's responsiveObserver requires it.
if (typeof window.matchMedia !== "function") {
  window.matchMedia = (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList;
}

// jsdom does not implement getComputedStyle for pseudo-elements and logs a
// "Not implemented" jsdomError each time antd (Table/Drawer/Tooltip)
// measures one. Return a real (element) style declaration instead so the
// measurement code gets a usable object and CI stderr stays clean.
const nativeGetComputedStyle = window.getComputedStyle.bind(window);
window.getComputedStyle = (elt: Element, pseudoElt?: string | null): CSSStyleDeclaration => {
  if (pseudoElt !== undefined && pseudoElt !== null) {
    return nativeGetComputedStyle(document.documentElement);
  }
  return nativeGetComputedStyle(elt);
};

// jsdom does not implement ResizeObserver; antd (Tabs/Dropdown/Drawer)
// measures elements with it. A no-op stub keeps layout effects silent.
if (typeof window.ResizeObserver !== "function") {
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// Vitest globals are disabled, so register Testing Library cleanup explicitly.
afterEach(() => {
  cleanup();
  // App tests share jsdom's history within a worker. Reset the Wave A
  // settings route so a test that opened the center cannot hide the next
  // test's Adapter console.
  if (window.location.pathname.startsWith("/settings")) {
    window.history.replaceState(null, "", "/");
  }
});
