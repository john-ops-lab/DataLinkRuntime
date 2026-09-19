export type ExecutionOutputKind = "truncated" | "content" | "json-null" | "empty" | "unknown";

const hasOwn = (value: object, key: string): boolean => Object.prototype.hasOwnProperty.call(value, key);

/** Classify persisted output without inventing facts for incomplete historical records. */
export function classifyExecutionOutput(execution: object): ExecutionOutputKind {
  const record = execution as Record<string, unknown>;
  const output = record.output;
  const size = record.output_size;
  const hasPreview = record.output_preview !== null && record.output_preview !== undefined;

  if (record.output_truncated === true) {
    return "truncated";
  }
  if (hasOwn(record, "output") && output !== null && output !== undefined) {
    return "content";
  }
  if (hasOwn(record, "output_truncated") && typeof record.output_truncated !== "boolean") {
    return "unknown";
  }

  const neverStartedIdentity =
    hasOwn(record, "dispatch_backend") &&
    record.dispatch_backend === "rabbitmq" &&
    hasOwn(record, "attempt_count") &&
    record.attempt_count === 0 &&
    hasOwn(record, "started_at") &&
    record.started_at === null &&
    hasOwn(record, "status") &&
    (record.status === "queued" || record.status === "cancelled" || record.status === "expired") &&
    hasOwn(record, "output") &&
    output === null &&
    !hasPreview;

  if (
    output === null &&
    hasOwn(record, "output") &&
    typeof size === "number" &&
    Number.isFinite(size) &&
    Number.isInteger(size) &&
    size === 4 &&
    !hasPreview &&
    !neverStartedIdentity
  ) {
    return "json-null";
  }

  const explicitEmptySize =
    hasOwn(record, "output_size") &&
    size === 0 &&
    (output === null || output === undefined) &&
    !hasPreview;
  const reliablyNeverStarted = neverStartedIdentity && (size === null || size === undefined);
  if (explicitEmptySize || reliablyNeverStarted) {
    return "empty";
  }
  return "unknown";
}
