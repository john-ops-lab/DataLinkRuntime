/** JSON has already been decoded by the browser; this cannot recover lost digits. */
export function hasUnsafeRuntimeConfigNumbers(value: unknown): boolean {
  const pending = [value];
  while (pending.length > 0) {
    const item = pending.pop();
    if (typeof item === "number") {
      if (!Number.isFinite(item) || (Number.isInteger(item) && !Number.isSafeInteger(item))) return true;
    } else if (item !== null && typeof item === "object") {
      for (const child of Object.values(item)) pending.push(child);
    }
  }
  return false;
}

export type RuntimeConfigDraft =
  | { kind: "valid" | "unsafe_number"; value: Record<string, unknown> }
  | { kind: "invalid" };

export function parseRuntimeConfigDraft(text: string): RuntimeConfigDraft {
  try {
    const value: unknown = JSON.parse(text);
    if (value === null || typeof value !== "object" || Array.isArray(value)) return { kind: "invalid" };
    return {
      kind: hasUnsafeRuntimeConfigNumbers(value) ? "unsafe_number" : "valid",
      value: value as Record<string, unknown>,
    };
  } catch {
    return { kind: "invalid" };
  }
}

export function runtimeConfigSnapshot(value: Record<string, unknown>) {
  // A very large server integer can decode to Infinity, then stringify to null.
  // Retain the warning before stringify so later code edits cannot save that null.
  const runtimeConfigUnsafeNumbers = hasUnsafeRuntimeConfigNumbers(value);
  return { runtimeConfigText: JSON.stringify(value, null, 2), runtimeConfigUnsafeNumbers };
}
