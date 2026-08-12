/** Lightweight Adapter reconciliation while a Production Execution is active.
 * Focused tests may shorten this interval without real-time sleeps. */
export const PRODUCTION_REFRESH_POLICY = { pollIntervalMs: 3000 };
