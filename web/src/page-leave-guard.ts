/** Imperative contract for UI surfaces that own local drafts or mutations. */
export interface PageLeaveGuardHandle {
  /** Confirm local draft loss, or return false while a mutation is in flight. */
  confirmLeave: () => boolean;
}
