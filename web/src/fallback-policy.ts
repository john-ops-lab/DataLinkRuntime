/**
 * Fallback policy for the test-run panel after any abnormal SSE end.
 *
 * The panel converges on the authoritative M2 result with bounded GET
 * polling: transient GET failures keep retrying inside the same budget,
 * and the cap covers long tasks, not only a few seconds. Kept in its own
 * module so tests can tighten the pace and the component file stays a
 * pure component export (react-refresh).
 */
export const FALLBACK_POLICY = { pollIntervalMs: 3000, maxPolls: 60 }; // 约 3 分钟有界等待
