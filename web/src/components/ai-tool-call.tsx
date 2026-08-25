/** M5.7 Wave C1: the official assistant-ui Tool Call UI for DLR's controlled
 * read-only tools.
 *
 * Rendered through `MessagePrimitive.Content components.tools` (the standard
 * `ToolCallMessagePartComponent` contract) inside the External Store
 * Runtime. Only sanitized, bounded data ever reaches this component:
 * DLR's backend builds the tool-call parts from `AiToolCallSummary` entries
 * (args/result summaries capped server-side, secrets redacted, hidden
 * reasoning never present), and the component itself clamps every string
 * before rendering as a second client-side bound.
 *
 * Accessibility: the whole card is one `role="status"` region with
 * `aria-label` identifying the tool and its state; the status text and every
 * label are localized (zh-CN / en) through the standard i18n contract. The
 * state is announced without adding a separate truncation notice.
 */

import { useTranslation } from "react-i18next";
import type { ToolCallMessagePartProps } from "@assistant-ui/react";

/** Second client-side bound; the server bound (400 chars) is authoritative,
 * this is a defensive render clamp so a hostile payload can never blow the
 * layout even if a future server version misbehaves. */
const DISPLAY_MAX_CHARS = 600;

function clampDisplay(value: string): string {
  if (value.length <= DISPLAY_MAX_CHARS) {
    return value;
  }
  if (value.endsWith("…")) {
    return `${value.slice(0, DISPLAY_MAX_CHARS - 1).replace(/…+$/u, "")}…`;
  }
  return value.slice(0, DISPLAY_MAX_CHARS);
}

/** True when the part is still running (no result yet) — the "calling"
 * state of the official Tool Call status contract. */
function isCalling(props: ToolCallMessagePartProps): boolean {
  return props.status.type === "running" || props.result === undefined;
}

/** True when the part carries the tool-error state: either the official
 * `isError` flag, an incomplete-with-error part status, or a DLR stable
 * error code in the result. */
function isErrorState(props: ToolCallMessagePartProps): boolean {
  if (props.isError === true) {
    return true;
  }
  if (props.status.type === "incomplete" && props.status.reason === "error") {
    return true;
  }
  if (typeof props.result === "string") {
    try {
      const parsed: unknown = JSON.parse(props.result);
      if (
        typeof parsed === "object" &&
        parsed !== null &&
        (parsed as { ok?: boolean }).ok === false
      ) {
        return true;
      }
    } catch {
      // not JSON — treat as a plain result string
    }
  }
  return false;
}

export function DlrToolCallUI(props: ToolCallMessagePartProps) {
  const { t } = useTranslation(["ai"]);
  const calling = isCalling(props);
  const failed = !calling && isErrorState(props);
  const rawName = clampDisplay(props.toolName || t("assistant.tools.unknownName"));
  // M5.7 Wave C2: knowledge tools get a localized display name; unknown
  // tools (and the C1 docs tools) fall back to the raw registered name.
  const toolName = clampDisplay(
    t(`assistant.tools.names.${rawName}`, { defaultValue: rawName }),
  );
  const argsText = clampDisplay(props.argsText ?? "");
  const resultText = clampDisplay(
    typeof props.result === "string" ? props.result : "",
  );
  const statusKey = calling
    ? "assistant.tools.status.calling"
    : failed
      ? "assistant.tools.status.error"
      : "assistant.tools.status.success";
  const statusLabel = t(statusKey);
  const errorCode = failed ? extractErrorCode(props.result, props.argsText) : null;
  const regionLabel = t("assistant.tools.ariaGroup", {
    name: toolName,
    status: statusLabel,
  });
  return (
    <div
      className={`ai-tool-call${failed ? " ai-tool-call-error" : ""}${calling ? " ai-tool-call-calling" : " ai-tool-call-success"}`}
      data-testid="ai-tool-call"
      role="status"
      aria-label={regionLabel}
    >
      <span className="ai-tool-name" data-testid="ai-tool-name">
        {toolName}
      </span>
      <span className="ai-tool-status" data-testid="ai-tool-status">
        {statusLabel}
      </span>
      {argsText !== "" && (
        <span className="ai-tool-args" data-testid="ai-tool-args">
          {t("assistant.tools.argsLabel")}: {argsText}
        </span>
      )}
      {!calling && (
        <span className="ai-tool-result" data-testid="ai-tool-result">
          {t("assistant.tools.resultLabel")}: {failed ? t("assistant.tools.rejected") : resultText}
        </span>
      )}
      {errorCode !== null && (
        <span className="ai-tool-error-code" data-testid="ai-tool-error-code">
          {t("assistant.tools.errorLabel", { code: errorCode })}
        </span>
      )}
    </div>
  );
}

/** Extract the stable DLR error code from the sanitized result JSON without
 * ever reflecting the raw result into the UI. Returns null when absent. */
function extractErrorCode(result: unknown, argsText: string): string | null {
  if (typeof result === "string") {
    try {
      const parsed: unknown = JSON.parse(result);
      if (typeof parsed === "object" && parsed !== null) {
        const code = (parsed as { error_code?: unknown }).error_code;
        if (typeof code === "string" && code.length > 0) {
          if (code.length <= 64) {
            return code;
          }
        }
      }
    } catch {
      // not JSON — no stable code
    }
  }
  void argsText;
  return null;
}
