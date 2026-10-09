import { useLayoutEffect, useRef } from "react";

/** Capture before the overlay's passive autofocus; restore after its exit motion. */
export function useOverlayFocus(open: boolean, fallbackSelector: string) {
  const trigger = useRef<HTMLElement | null>(null);
  const pendingFrame = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (pendingFrame.current !== null) cancelAnimationFrame(pendingFrame.current);
    if (open && document.activeElement instanceof HTMLElement) {
      trigger.current = document.activeElement;
    }
    if (!open && trigger.current !== null) {
      restoreFocus(false);
    }
    return () => {
      if (pendingFrame.current !== null) cancelAnimationFrame(pendingFrame.current);
      // Some callers conditionally unmount the open overlay on close.
      if (trigger.current !== null) restoreFocus(false);
    };
    // The fallback is fixed by the caller; use the latest committed DOM on close.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  function restoreFocus(visible: boolean) {
    if (visible) return;
    if (pendingFrame.current !== null) cancelAnimationFrame(pendingFrame.current);
    // rc-drawer restores its own saved element after afterOpenChange. Apply
    // our checked target on the next frame, after that native restoration.
    pendingFrame.current = requestAnimationFrame(() => {
      pendingFrame.current = null;
      applyFocus();
    });
  }
  function applyFocus() {
    const previous = trigger.current;
    const available = (element: HTMLElement) => element.isConnected && element.getClientRects().length > 0 &&
      (element.tabIndex >= 0 || element.hasAttribute("tabindex")) &&
      !(element instanceof HTMLButtonElement && element.disabled) && element.getAttribute("aria-disabled") !== "true";
    // A keyed portal may autofocus during open. Its still-connected exiting
    // nodes are never an external trigger, even while they have layout boxes.
    const target = previous !== null && available(previous) &&
      !previous.closest(".ant-drawer, .ant-modal, .ant-dropdown")
      ? previous : [...document.querySelectorAll<HTMLElement>(fallbackSelector)].find(available);
    target?.focus();
  }
  return restoreFocus;
}
